"""Reminders service — day-before DJ slot reminders.

Handles: FR29 — day-before reminder scheduling and delivery.

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
[Source: architecture.md#Error Routing: Centralized AdminLogService]
"""
import datetime
import logging
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any

import disnake

from bot.db.bookings import get_booked_djs_for_event
from bot.db.config import get_value
from bot.db.interventions import is_under_intervention
from bot.db.reminders import has_reminder_been_sent, record_reminder_sent
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    import asyncpg

    from bot.log import AdminLogService

logger = logging.getLogger(__name__)


class RemindersService:
    """Stateless day-before reminder service.

    Called by EventsCog._check_reminders() every hour.
    All state stored in PostgreSQL (reminders_sent table) — no per-instance state.

    [Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
    """

    def __init__(
        self,
        bot: Any,
        db: "asyncpg.Pool",
        notion_client: Any,
        log_service: "AdminLogService | None",
    ) -> None:
        self.bot = bot
        self.db = db
        self.notion_client = notion_client
        self.log_service = log_service

    @staticmethod
    def _event_midnight_utc(event: dict) -> datetime.datetime | None:
        """Parse event Date property → midnight UTC datetime.

        Notion Date field is a date string (YYYY-MM-DD). We treat midnight UTC
        of that date as the reference point for the 20-28 hour window check.

        Returns None if the Date property is missing or malformed.

        [Source: epics.md#Story 5.4 AC1 — 'events occurring within the next 20-28 hours']
        """
        try:
            date_str = event["properties"]["Date"]["date"]["start"]
            d = datetime.date.fromisoformat(date_str)
            return datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=UTC)
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _is_in_reminder_window(
        event: dict,
        now: datetime.datetime,
    ) -> bool:
        """Return True if event midnight UTC is 20-28 hours from now.

        The 8-hour window ensures the hourly task fires at least once per event
        while keeping reminders in the 'day before' range.

        Takes 'now' explicitly so callers can inject a fixed time for testing
        without patching datetime.

        [Source: epics.md#Story 5.4 AC1]
        """
        event_dt = RemindersService._event_midnight_utc(event)
        if event_dt is None:
            return False
        delta = event_dt - now
        return timedelta(hours=20) <= delta <= timedelta(hours=28)

    async def _compute_slot_unix(
        self,
        event: dict,
        slot_index: int,
        guild_id: int,
    ) -> int:
        """Compute the unix timestamp for a specific slot.

        Uses event date + config default_start_time + slot_index * slot_duration_minutes.
        Fallbacks: default_start_time='20:00', slot_duration_minutes=60.

        These config keys are set via /config commands (Epic 7). Until then, the
        fallback values produce a reasonable 8-PM-based slot time.

        [Source: epics.md#Story 5.4 AC2 — <t:{unix}:f> in reminder DM]
        [Source: architecture.md#Config Storage: PostgreSQL Config Table]
        """
        event_dt = self._event_midnight_utc(event)
        if event_dt is None:
            return int(datetime.datetime.now(UTC).timestamp())

        start_time_str = await get_value(self.db, guild_id, "default_start_time") or "20:00"
        slot_dur_str = await get_value(self.db, guild_id, "slot_duration_minutes") or "60"

        try:
            hour, minute = (int(x) for x in start_time_str.split(":"))
        except (ValueError, AttributeError):
            hour, minute = 20, 0

        try:
            slot_duration = int(slot_dur_str)
        except (ValueError, TypeError):
            slot_duration = 60

        event_start = event_dt.replace(hour=hour, minute=minute)
        slot_time = event_start + timedelta(minutes=slot_index * slot_duration)
        return int(slot_time.timestamp())

    async def send_reminders(
        self,
        guild_id: int,
        ctx: Any,  # noqa: ARG002
        _now: datetime.datetime | None = None,
    ) -> None:
        """Send day-before reminders to all booked DJs for tomorrow's events.

        Algorithm:
          1. Fetch all future events from Notion.
          2. Filter to events whose midnight UTC is 20-28 hours from now.
          3. For each matching event, fetch booked DJs from slot_locks JOIN djs.
          4. For each booked DJ, skip if reminder already sent (AC2, AC4).
          5. Compute slot_unix; send DM.
          6. On success: record reminder sent (prevents duplicate on next cycle).
          7. On discord.Forbidden: post ERR_DM_FAILED, do NOT record (allows retry
             on subsequent cycles still within the 20-28h window).

        ctx: BotContext — accepted for API consistency with other service methods.
        guild_id: used for config DB lookups (default_start_time, slot_duration_minutes).
        _now: injectable for testing; defaults to datetime.now(UTC).

        [Source: epics.md#Story 5.4 AC1-AC4]
        [Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
        """
        # Step 1: fetch events
        try:
            events = await self.notion_client.poll_events()
        except Exception as exc:
            logger.warning("RemindersService.send_reminders: Notion poll failed: %s", exc)
            return

        # Step 2: filter to 20-28h window (compute 'now' once for the whole cycle)
        now = _now if _now is not None else datetime.datetime.now(UTC)
        upcoming = [e for e in events if self._is_in_reminder_window(e, now)]
        if not upcoming:
            return

        reminder_count = 0

        for event in upcoming:
            event_id = event.get("id", "")
            if not event_id:
                continue

            # Step 3: booked DJs for this event
            try:
                booked_djs = await get_booked_djs_for_event(self.db, event_id)
            except Exception as exc:
                logger.warning(
                    "RemindersService.send_reminders: DB error for event %s: %s",
                    event_id,
                    exc,
                )
                continue

            for dj in booked_djs:
                discord_user_id: int = dj["discord_user_id"]
                dj_name: str = dj.get("dj_name") or str(discord_user_id)
                slot_index: int = dj["slot_index"]

                # Step 4: skip if already sent (AC2, AC4)
                try:
                    if await has_reminder_been_sent(
                        self.db, event_id, discord_user_id, slot_index
                    ):
                        continue
                except Exception as exc:
                    logger.warning(
                        "RemindersService.send_reminders: DB check failed for DJ %s: %s",
                        discord_user_id,
                        exc,
                    )
                    continue

                # Step 5: compute slot time
                try:
                    slot_unix = await self._compute_slot_unix(event, slot_index, guild_id)
                except Exception as exc:
                    logger.warning(
                        "RemindersService.send_reminders: slot_unix failed for DJ %s: %s",
                        discord_user_id,
                        exc,
                    )
                    slot_unix = int(now.timestamp())

                # Story 6.2 AC7: skip DJs under admin intervention silently
                try:
                    if await is_under_intervention(self.db, discord_user_id):
                        logger.info(
                            "RemindersService.send_reminders: skipping DJ %s (%s) — under admin intervention",
                            dj_name,
                            discord_user_id,
                        )
                        continue
                except Exception as exc:
                    logger.warning(
                        "RemindersService.send_reminders: intervention check failed for %s: %s",
                        discord_user_id,
                        exc,
                    )

                # Get Discord user from cache
                user = self.bot.get_user(discord_user_id)
                if user is None:
                    logger.warning(
                        "RemindersService.send_reminders: user %s not in cache — skipping",
                        discord_user_id,
                    )
                    continue

                # Step 6/7: send DM
                dm_text = (
                    f"Heads up \u2014 you're booked tomorrow at <t:{slot_unix}:f>. "
                    "Get ready to drop some bangers! \U0001f3a7"
                )
                try:
                    await user.send(dm_text)
                    # Story 6.1 AC1: log outgoing reminder DM
                    if self.log_service is not None:
                        await self.log_service.post_dm_transcript(
                            dj_name=dj_name,
                            direction="BOT",
                            content=dm_text,
                        )
                except disnake.Forbidden:
                    # AC3: DMs blocked — log error, do NOT record sent (allow retry next cycle)
                    if self.log_service is not None:
                        try:
                            await self.log_service.post_error(
                                error_code=ErrorCode.ERR_DM_FAILED,
                                trigger=(
                                    f"Day-before reminder DM failed for {dj_name} "
                                    f"(ID: {discord_user_id}) \u2014 slot <t:{slot_unix}:f>"
                                ),
                                action_taken="Reminder not delivered. User has DMs blocked.",
                                manual_action=(
                                    f"Manually notify {dj_name} about their booking at "
                                    f"<t:{slot_unix}:f>."
                                ),
                            )
                        except Exception as log_exc:
                            logger.warning(
                                "RemindersService.send_reminders: post_error failed: %s",
                                log_exc,
                            )
                    continue
                except Exception as exc:
                    logger.warning(
                        "RemindersService.send_reminders: unexpected DM error for %s: %s",
                        discord_user_id,
                        exc,
                    )
                    continue

                # Record sent (AC2, AC4) — only on successful delivery
                try:
                    await record_reminder_sent(self.db, event_id, discord_user_id, slot_index)
                    reminder_count += 1
                except Exception as exc:
                    logger.warning(
                        "RemindersService.send_reminders: failed to record for DJ %s: %s",
                        discord_user_id,
                        exc,
                    )

        logger.info(
            "RemindersService.send_reminders: %d reminder(s) sent across %d upcoming event(s)",
            reminder_count,
            len(upcoming),
        )
