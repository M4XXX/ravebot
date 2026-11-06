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
from typing import Any

import asyncpg
from disnake.ext import commands, tasks

from bot.context import BotContext
from bot.db.bookings import init_event_slots
from bot.db.config import get_value, set_value
from bot.notion.client import DuplicateEventError, NotionWriteError
from bot.notion.models import EventInfo
from bot.services.event_defaults import default_event_shape
from bot.ui.embeds import ErrorCode

logger = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    """Notion polling, lineup post updates, and reminder scheduling."""

    bot: commands.Bot  # injected by disnake when the cog is added

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

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
    def _build_snapshot(events: list[EventInfo]) -> str:
        """Stable, content-based JSON snapshot of events and their slot assignments.

        One entry per event (sorted by page_id), including the list of
        [slot number, dj_page_id] pairs sorted by slot number. Order-independent
        and changes whenever an assignment or schedule field changes.

        [Source: epics.md#Story 3.1 AC1]
        """
        entries: list[dict[str, Any]] = sorted(
            (
                {
                    "page_id": e.page_id,
                    "event_id": e.event_id,
                    "date": e.date.isoformat(),
                    "start": e.start.strftime("%H:%M"),
                    "slot_count": e.slot_count,
                    "slot_minutes": e.slot_minutes,
                    "status": e.status,
                    "slots": [
                        [s.number, s.dj_page_id]
                        for s in sorted(e.slots, key=lambda s: s.number)
                    ],
                }
                for e in events
            ),
            key=lambda x: x["page_id"],
        )
        return json.dumps(entries)

    def _get_guild_id(self) -> int | None:
        """Return the first guild's ID, or None if the bot has no guilds yet.

        [Source: epics.md#Story 3.1 AC1]
        """
        if not self.bot.guilds:
            logger.warning("_get_guild_id: bot has no guilds — skipping poll cycle")
            return None
        return self.bot.guilds[0].id

    async def _auto_create_events(self, events: list[EventInfo], guild_id: int) -> None:
        """Create Notion events to maintain the configured look-ahead count.

        Reads event_lookahead_count (default 6) and event_interval_days (default 7)
        from config; start, slot length and slot count come from
        default_event_shape() (config, not the latest event). Finds the latest
        existing event date and creates new events spaced by interval_days until the count reaches
        the look-ahead target. After each successful Notion write, pre-initializes
        slot_locks rows via init_event_slots() (Story 7.3 AC2).

        Called from _poll_notion() after list_events() succeeds, before snapshot
        comparison — newly created events appear in the same cycle's snapshot.

        On NotionWriteError: ERR_NOTION_WRITE is already posted by NotionApi.request.
        Logs a warning and continues — remaining events in the loop still attempted.

        [Source: epics.md#Story 7.1 AC1-AC4]
        """
        raw_lookahead = await get_value(self.ctx.db, guild_id, "event_lookahead_count")
        lookahead = int(raw_lookahead) if raw_lookahead else 6

        if len(events) >= lookahead:
            return  # AC4: already at or above target, no action

        raw_interval = await get_value(self.ctx.db, guild_id, "event_interval_days")
        interval_days = int(raw_interval) if raw_interval else 7

        # Find latest event date; events are sorted ascending by list_events()
        latest_date: datetime.date = events[-1].date if events else datetime.date.today()

        client = self.ctx.notion_client
        db = self.ctx.db
        if client is None or db is None:
            return

        # Start, slot length and slot count come from config (Ruling 25)
        shape = await default_event_shape(db, guild_id)

        events_to_create = lookahead - len(events)
        for i in range(1, events_to_create + 1):
            new_date = latest_date + timedelta(days=interval_days * i)
            event_id = f"{new_date.isoformat()} Rave Night"
            try:
                if await client.event_id_exists(event_id):
                    logger.info("Auto-create skipped: event ID %r already exists", event_id)
                    continue
                event = await client.create_event(
                    event_id, new_date, shape.start, shape.slot_count, shape.slot_minutes
                )
                logger.info("Auto-created Notion event: %r", event_id)
                # Pre-initialize slot_locks rows (Story 7.3 AC2)
                try:
                    await init_event_slots(db, event.page_id, shape.slot_count)
                except Exception as exc:
                    logger.warning(
                        "init_event_slots failed for event %r: %s"
                        " — slots will be lazily created on booking",
                        event_id,
                        exc,
                    )
            except (NotionWriteError, DuplicateEventError):
                logger.warning(
                    "Auto event creation failed for %s — ERR_NOTION_WRITE posted to admin log",
                    new_date.isoformat(),
                )
            except ValueError:
                logger.exception("Auto event creation rejected for %s", new_date.isoformat())

    async def _trigger_lineup_update(self, events: list[EventInfo]) -> None:
        """Trigger lineup channel post update via LineupService.

        [Source: epics.md#Story 3.2]
        """
        if self.ctx.lineup_service is None:
            logger.warning("lineup_service not available — skipping lineup update")
            return
        await self.ctx.lineup_service.post_update(events)

    async def _run_sync_cycle(self, guild_id: int) -> None:
        """Run one NotionSyncService cycle; a failure is logged and never raised.

        The lineup update must not depend on the sync succeeding.
        """
        sync = self.ctx.notion_sync_service
        if sync is None:
            return
        try:
            await sync.run_cycle(guild_id)
        except Exception as exc:
            logger.warning("Notion sync cycle failed — continuing with the poll: %s", exc)
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger=f"NotionSyncService.run_cycle() raised during notion_poll: {exc}",
                    action_taken="Skipped the sync this cycle — lineup update continues.",
                    manual_action=(
                        "Check NOTION_API_KEY, NOTION_PARENT_PAGE_ID, and Notion API status."
                    ),
                )

    @tasks.loop(minutes=5)
    async def _poll_notion(self) -> None:
        """Notion event polling — syncs hand edits, fetches events, updates the lineup.

        Order per cycle: (1) NotionSyncService.run_cycle (skipped if the service is
        None; a failure is posted and the cycle continues), (2) list_events() for the
        state after the sync, (3) auto-create events, (4) snapshot and lineup update.
        The first iteration runs after the bot is ready and is the startup reconcile.

        On DB unavailability: skips cycle (guard handles logging).
        On Notion read failure: logs warning, posts ERR_NOTION_WRITE, skips cycle
        (no snapshot update).
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

        # Reconcile hand edits in Notion with Postgres before reading the events
        await self._run_sync_cycle(guild_id)

        # Fetch events from Notion (AC1, AC4)
        try:
            events = await self.ctx.notion_client.list_events()
        except Exception as exc:
            logger.warning("Notion poll failed — skipping cycle: %s", exc)
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger=f"Notion list_events() raised during notion_poll: {exc}",
                    action_taken="Skipped this cycle — will retry on next scheduled run.",
                    manual_action=(
                        "Check NOTION_API_KEY, NOTION_PARENT_PAGE_ID, and Notion API status."
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

        # AC2: change detected — trigger lineup post then persist new snapshot
        await self._trigger_lineup_update(events)
        await set_value(self.ctx.db, guild_id, "lineup_snapshot", new_snapshot)

    @_poll_notion.before_loop
    async def _before_poll_notion(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def _check_reminders(self) -> None:
        """Reminder scheduler loop — delegates to RemindersService.send_reminders.

        Guard ensures DB is available before any DB-dependent work.
        """
        if not await self._guard_db("reminder_check"):
            return
        guild_id = self._get_guild_id()
        if guild_id is None:
            return
        if self.ctx.reminders_service is None:
            logger.warning("_check_reminders: reminders_service not available — skipping")
            return
        try:
            await self.ctx.reminders_service.send_reminders(guild_id, self.ctx)
        except Exception as exc:
            logger.warning("_check_reminders: send_reminders raised: %s", exc)

    @_check_reminders.before_loop
    async def _before_check_reminders(self) -> None:
        await self.bot.wait_until_ready()
