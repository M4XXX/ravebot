"""Events cog — Notion poll loop, lineup post, day-before reminders.

Story 1.5: DB unavailability guard pattern for scheduled task loops.
Full Notion integration (FR9-13) implemented in Story 3.x.

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: epics.md#Story 1.5 AC1]
"""
import logging

import asyncpg
from disnake.ext import commands, tasks

from bot.context import BotContext
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

    @tasks.loop(minutes=5)
    async def _poll_notion(self) -> None:
        """Notion event polling loop — full implementation in Story 3.x.

        Guard ensures DB is available before any DB-dependent work.
        """
        if not await self._guard_db("notion_poll"):
            return
        # Story 3.x: query Notion for event updates, update DB, post lineup

    @tasks.loop(hours=1)
    async def _check_reminders(self) -> None:
        """Day-before reminder scheduler loop — full implementation in Story 3.x.

        Guard ensures DB is available before any DB-dependent work.
        """
        if not await self._guard_db("reminder_check"):
            return
        # Story 3.x: check upcoming events, send day-before DMs
