"""Events cog — Notion poll loop, lineup post, day-before reminders.

Story 1.5: DB unavailability guard pattern for scheduled task loops.
Story 3.1: Notion event polling with snapshot-based change detection.
Full lineup post (FR10) implemented in Story 3.2.

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: epics.md#Story 1.5 AC1]
[Source: epics.md#Story 3.1 AC1-AC4]
"""
import json
import logging

import asyncpg
from disnake.ext import commands, tasks

from bot.context import BotContext
from bot.db.config import get_value, set_value
from bot.ui.embeds import ErrorCode

logger = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    """Notion polling, lineup post updates, and reminder scheduling."""

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
        """Return the first guild's ID, or None if the bot has no guilds yet.

        [Source: epics.md#Story 3.1 AC1]
        """
        if not self.bot.guilds:
            logger.warning("_get_guild_id: bot has no guilds — skipping poll cycle")
            return None
        return self.bot.guilds[0].id

    async def _trigger_lineup_update(self, events: list[dict]) -> None:
        """Trigger lineup channel post update via LineupService.

        [Source: epics.md#Story 3.2]
        """
        if self.ctx.lineup_service is None:
            logger.warning("lineup_service not available — skipping lineup update")
            return
        await self.ctx.lineup_service.post_update(events)

    @tasks.loop(minutes=5)
    async def _poll_notion(self) -> None:
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

        # Compare against stored snapshot (AC1, AC2, AC3)
        new_snapshot = self._build_snapshot(events)
        stored_snapshot = await get_value(self.ctx.db, guild_id, "lineup_snapshot")

        if new_snapshot == stored_snapshot:
            return  # AC3: no change, no action

        # AC2: change detected — trigger lineup post then persist new snapshot
        await self._trigger_lineup_update(events)
        await set_value(self.ctx.db, guild_id, "lineup_snapshot", new_snapshot)

    @tasks.loop(hours=1)
    async def _check_reminders(self) -> None:
        """Day-before reminder scheduler loop — full implementation in Story 3.x.

        Guard ensures DB is available before any DB-dependent work.
        """
        if not await self._guard_db("reminder_check"):
            return
        # Story 3.x: check upcoming events, send day-before DMs
