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
from bot.db.reminders import has_reminder_been_sent, record_reminder_sent
from bot.notion.models import EventInfo
from bot.ui import texts
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
    def _is_in_reminder_window(
        event: EventInfo,
        now: datetime.datetime,
    ) -> bool:
        """Return True if the event start is 20-28 hours from now.

        The 8-hour window ensures the hourly task fires at least once per event
        while keeping reminders in the 'day before' range.

        Takes 'now' explicitly so callers can inject a fixed time for testing
        without patching datetime.

        [Source: epics.md#Story 5.4 AC1]
        """
        delta = event.slot_start(0) - now
        return timedelta(hours=20) <= delta <= timedelta(hours=28)

    async def send_reminders(
        self,
        guild_id: int,
        ctx: Any,  # noqa: ARG002
        _now: datetime.datetime | None = None,
    ) -> None:
        """Send day-before reminders to all booked DJs for tomorrow's events.

        Algorithm:
          1. Fetch all future events from Notion.
          2. Filter to events whose start is 20-28 hours from now.
          3. For each matching event, fetch booked DJs from slot_locks JOIN djs.
          4. For each booked DJ, skip if reminder already sent (AC2, AC4).
          5. Compute slot_unix; send DM.
          6. On success: record reminder sent (prevents duplicate on next cycle).
          7. On discord.Forbidden: post ERR_DM_FAILED, do NOT record (allows retry
             on subsequent cycles still within the 20-28h window).

        ctx: BotContext — accepted for API consistency with other service methods.
        guild_id: accepted for API consistency (slot times come from the event itself).
        _now: injectable for testing; defaults to datetime.now(UTC).

        [Source: epics.md#Story 5.4 AC1-AC4]
        [Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
        """
        # Step 1: fetch events
        try:
            events = await self.notion_client.list_events()
        except Exception as exc:
            logger.warning("RemindersService.send_reminders: list_events failed: %s", exc)
            return

        # Step 2: filter to 20-28h window (compute 'now' once for the whole cycle)
        now = _now if _now is not None else datetime.datetime.now(UTC)
        upcoming = [e for e in events if self._is_in_reminder_window(e, now)]
        if not upcoming:
            return

        reminder_count = 0

        for event in upcoming:
            event_id = event.page_id
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

                if discord_user_id <= 0:
                    continue  # guest DJ: no Discord account, no reminder

                # Get Discord user from cache
                user = self.bot.get_user(discord_user_id)
                if user is None:
                    logger.warning(
                        "RemindersService.send_reminders: user %s not in cache — skipping",
                        discord_user_id,
                    )
                    if self.log_service is not None:
                        try:
                            await self.log_service.post_error(
                                error_code=ErrorCode.ERR_DM_FAILED,
                                trigger=(
                                    f"The reminder for {dj_name} could not be delivered "
                                    f"({event.event_id}, "
                                    f"{texts.slot_phrase(event, slot_index)}) "
                                    "because the bot cannot find them on the server."
                                ),
                                action_taken="The bot tries again on the next run.",
                                manual_action=texts.dm_failed_todo(dj_name),
                            )
                        except Exception as log_exc:
                            logger.warning(
                                "RemindersService.send_reminders: post_error failed: %s",
                                log_exc,
                            )
                    continue

                # Step 6/7: send DM
                dm_text = texts.reminder(event, slot_index)
                try:
                    await user.send(dm_text)
                except disnake.Forbidden:
                    # AC3: DMs blocked — log error, do NOT record sent (allow retry next cycle)
                    if self.log_service is not None:
                        try:
                            await self.log_service.post_error(
                                error_code=ErrorCode.ERR_DM_FAILED,
                                trigger=(
                                    f"The reminder for {dj_name} could not be delivered "
                                    f"({event.event_id}, "
                                    f"{texts.slot_phrase(event, slot_index)}) "
                                    "because their DMs are closed."
                                ),
                                action_taken="The bot tries again on the next run.",
                                manual_action=texts.dm_failed_todo(dj_name),
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
