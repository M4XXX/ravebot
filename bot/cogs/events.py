"""Events cog — Notion poll loop, lineup post, day-before reminders.

Story 1.5: DB unavailability guard pattern for scheduled task loops.
Story 3.1: Notion event polling with snapshot-based change detection.
Story 7.1: _auto_create_events() — maintain look-ahead event count in Notion.
Full lineup post (FR10) implemented in Story 3.2.

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: epics.md#Story 1.5 AC1]
[Source: epics.md#Story 3.1 AC1-AC4]
[Source: epics.md#Story 7.1 AC1-AC4]
"""
import datetime
import json
import logging
from datetime import timedelta

import asyncpg
from disnake.ext import commands, tasks

from bot import active, loops, schedule
from bot import guild as served
from bot.context import BotContext
from bot.db.bookings import init_event_slots
from bot.db.config import get_value, set_value
from bot.notion.client import NotionWriteError
from bot.ui.embeds import ErrorCode

logger = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    """Notion polling, lineup post updates, and reminder scheduling."""

    def __init__(self, ctx: BotContext, bot: commands.Bot) -> None:
        self.ctx = ctx
        self.bot = bot

    def cog_load(self) -> None:
        self._poll_notion.start()
        self._check_reminders.start()

    def cog_unload(self) -> None:
        self._poll_notion.cancel()
        self._check_reminders.cancel()

    async def _guard_db(self, operation: str) -> bool:
        """Return True if DB is available; post ERR_DB_UNAVAILABLE and return False if not.

        Call at the start of every scheduled task that requires DB access.
        On False: caller must return immediately — loop will retry next cycle.

        [Source: epics.md#Story 1.5 AC1]
        [Source: architecture.md#Error Routing: Centralized AdminLogService]
        """
        if self.ctx.db is None:
            logger.warning("DB pool not available during %s — skipping cycle", operation)
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_DB_UNAVAILABLE,
                    trigger=f"DB pool is None at start of {operation}",
                    action_taken="Skipped this cycle — will retry on next scheduled run.",
                    manual_action="Check PostgreSQL connectivity and POSTGRES_URL.",
                )
            return False
        try:
            await self.ctx.db.fetchval("SELECT 1")
            return True
        except (asyncpg.PostgresConnectionError, OSError) as exc:
            logger.warning("DB unavailable during %s: %s", operation, exc)
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_DB_UNAVAILABLE,
                    trigger=f"PostgreSQL connection failed during {operation}: {exc}",
                    action_taken="Paused this cycle — will retry on next scheduled run.",
                    manual_action="Check PostgreSQL connectivity.",
                )
            return False

    @staticmethod
    def _build_snapshot(events: list[dict]) -> str:
        """Stable JSON snapshot of event IDs + last_edited_time, sorted by id.

        Sorting by id ensures the result is order-independent — two fetches
        returning the same events in different order produce identical snapshots.

        [Source: epics.md#Story 3.1 AC1]
        """
        entries = sorted(
            [{"id": e["id"], "last_edited_time": e["last_edited_time"]} for e in events],
            key=lambda x: x["id"],
        )
        return json.dumps(entries)

    def _get_guild_id(self) -> int | None:
        """Return the served guild ID (bot.guild), or None if the bot serves none."""
        guild_id = served.resolve(self.bot)
        if guild_id is None:
            logger.warning("_get_guild_id: no served guild — skipping poll cycle")
        return guild_id

    async def _auto_create_events(self, events: list[dict], guild_id: int) -> None:
        """Create Notion events to maintain the configured look-ahead count.

        Reads event_lookahead_count (default 0 = off), event_interval_days (default 7),
        and slots_per_event (default 6) from config. Finds the latest existing event
        date and creates new events spaced by interval_days until the count reaches
        the look-ahead target. After each successful Notion write, pre-initializes
        slot_locks rows via init_event_slots() (Story 7.3 AC2).

        Called from _poll_notion() after poll_events() succeeds, before snapshot
        comparison — newly created events appear in the same cycle's snapshot.

        On NotionWriteError: ERR_NOTION_WRITE is already posted by _write_with_retry.
        Logs a warning and continues — remaining events in the loop still attempted.

        [Source: epics.md#Story 7.1 AC1-AC4]
        """
        raw_lookahead = await get_value(self.ctx.db, guild_id, "event_lookahead_count")
        lookahead = int(raw_lookahead) if raw_lookahead else 0
        if lookahead <= 0:
            return  # auto-creation is off (default): events come from /add-event or Notion

        if len(events) >= lookahead:
            return  # AC4: already at or above target, no action

        raw_interval = await get_value(self.ctx.db, guild_id, "event_interval_days")
        interval_days = int(raw_interval) if raw_interval else 7

        raw_slots = await get_value(self.ctx.db, guild_id, "slots_per_event")
        slots_per_event = int(raw_slots) if raw_slots else 6

        # Find latest event date; events are sorted ascending by poll_events()
        latest_date: datetime.date = datetime.date.today()
        if events:
            try:
                date_str = events[-1]["properties"]["Date"]["date"]["start"]
                latest_date = datetime.date.fromisoformat(date_str[:10])
            except (KeyError, TypeError, ValueError):
                pass  # fallback to today

        events_to_create = lookahead - len(events)
        for i in range(1, events_to_create + 1):
            new_date = latest_date + timedelta(days=interval_days * i)
            event_name = f"Rave Night {new_date.strftime('%d %b %Y')}"
            try:
                event_id = await self.ctx.notion_client.create_event(
                    event_name, new_date, slots=slots_per_event
                )
                logger.info(
                    "Auto-created Notion event: %r on %s", event_name, new_date.isoformat()
                )
                # Pre-initialize slot_locks rows (Story 7.3 AC2)
                try:
                    await init_event_slots(self.ctx.db, event_id, slots_per_event)
                except Exception as exc:
                    logger.warning(
                        "init_event_slots failed for event %r: %s"
                        " — slots will be lazily created on booking",
                        event_id,
                        exc,
                    )
            except NotionWriteError:
                logger.warning(
                    "Auto event creation failed for %s — ERR_NOTION_WRITE posted to admin log",
                    new_date.isoformat(),
                )

    async def _trigger_lineup_update(self, events: list[dict]) -> bool:
        """Trigger lineup channel post update via LineupService.

        [Source: epics.md#Story 3.2]
        """
        if self.ctx.lineup_service is None:
            logger.warning("lineup_service not available — skipping lineup update")
            return False
        return bool(await self.ctx.lineup_service.post_update(events))

    @tasks.loop(minutes=5)
    async def _poll_notion(self) -> None:
        await loops.guarded("notion_poll", self._poll_notion_once, self.ctx.log_service)

    async def _poll_notion_once(self) -> None:
        """Notion event polling — fetches future events, compares snapshot, triggers lineup update.

        On DB unavailability: skips cycle (guard handles logging).
        On Notion failure: logs warning, posts ERR_NOTION_WRITE, skips cycle (no snapshot update).
        On no change: silently returns.
        On change: triggers lineup update and persists new snapshot.

        [Source: epics.md#Story 3.1 AC1-AC4]
        [Source: architecture.md#Scheduled Work: disnake tasks.loop]
        """
        if not await self._guard_db("notion_poll"):
            return

        guild_id = self._get_guild_id()
        if guild_id is None:
            return
        await schedule.refresh(self.ctx.db, guild_id)
        if not await active.is_active(self.ctx.db, guild_id):
            return  # passive: no lineup posts, no auto-created events

        # Fetch events from Notion (AC1, AC4)
        try:
            events = await self.ctx.notion_client.poll_events()
        except Exception as exc:
            logger.warning("Notion poll failed — skipping cycle: %s", exc)
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger=f"Notion poll_events() raised during notion_poll: {exc}",
                    action_taken="Skipped this cycle — will retry on next scheduled run.",
                    manual_action=(
                        "Check NOTION_API_KEY, NOTION_DATABASE_ID, and Notion API status."
                    ),
                )
            return

        # Story 7.1: auto-create events to maintain look-ahead count (AC1-AC4)
        await self._auto_create_events(events, guild_id)

        # Compare against stored snapshot (AC1, AC2, AC3)
        new_snapshot = self._build_snapshot(events)
        stored_snapshot = await get_value(self.ctx.db, guild_id, "lineup_snapshot")

        if new_snapshot == stored_snapshot:
            return  # AC3: no change, no action

        # AC2: change detected — persist the snapshot only once the lineup shows it,
        # so a failed or capped post is retried on the next cycle
        if await self._trigger_lineup_update(events):
            await set_value(self.ctx.db, guild_id, "lineup_snapshot", new_snapshot)

    @tasks.loop(hours=1)
    async def _check_reminders(self) -> None:
        await loops.guarded("reminders", self._check_reminders_once, self.ctx.log_service)

    async def _check_reminders_once(self) -> None:
        """Day-before reminder scheduler loop — full implementation in Story 3.x.

        Guard ensures DB is available before any DB-dependent work.
        """
        if not await self._guard_db("reminder_check"):
            return
        guild_id = self._get_guild_id()
        if guild_id is None:
            return
        if not await active.is_active(self.ctx.db, guild_id):
            return  # passive: no reminder DMs
        if self.ctx.reminders_service is None:
            logger.warning("_check_reminders: reminders_service not available — skipping")
            return
        try:
            await self.ctx.reminders_service.send_reminders(guild_id, self.ctx)
        except Exception as exc:
            logger.warning("_check_reminders: send_reminders raised: %s", exc)

    @_poll_notion.before_loop
    @_check_reminders.before_loop
    async def _wait_until_ready(self) -> None:
        """Start the loops only once the gateway knows the guilds."""
        await self.bot.wait_until_ready()
