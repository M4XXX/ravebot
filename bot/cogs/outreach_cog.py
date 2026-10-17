"""Outreach cog — proactive outreach scheduler.

Story 1.5: DB unavailability guard pattern for scheduled task loop.
Full outreach implementation (FR25-30) in Story 5.x.

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: epics.md#Story 1.5 AC1]
"""
import logging

import asyncpg
from disnake.ext import commands, tasks

from bot.context import BotContext
from bot.ui.embeds import ErrorCode

logger = logging.getLogger(__name__)


class OutreachCog(commands.Cog):
    """Hosts the proactive outreach scheduler loop."""

    def __init__(self, ctx: BotContext, bot: commands.Bot) -> None:
        self.ctx = ctx
        self.bot = bot

    def cog_load(self) -> None:
        self._outreach_check.start()

    def cog_unload(self) -> None:
        self._outreach_check.cancel()

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

    def _get_guild_id(self) -> int | None:
        """Return the first guild's ID, or None if the bot has no guilds yet.

        [Source: bot/cogs/events.py#_get_guild_id — mirrors this exact pattern]
        """
        if not self.bot.guilds:
            logger.warning("OutreachCog._get_guild_id: bot has no guilds — skipping cycle")
            return None
        return self.bot.guilds[0].id

    @tasks.loop(minutes=30)
    async def _outreach_check(self) -> None:
        """Outreach gap detection — find events with open slots, manage sequences.

        Story 5.1: gap detection + sequence lifecycle (upsert, complete, cooldown).
        Story 5.2: DJ selection and DM delivery.
        Story 5.3: exhaustion detection, alert, and new-DJ reactivation.

        [Source: epics.md#Story 5.1 AC1–AC4]
        [Source: architecture.md#Scheduled Work: disnake tasks.loop(minutes=30)]
        """
        if not await self._guard_db("outreach_check"):
            return

        guild_id = self._get_guild_id()
        if guild_id is None:
            return

        if self.ctx.notion_client is None:
            logger.warning(
                "OutreachCog._outreach_check: notion_client not available — skipping cycle"
            )
            return

        if self.ctx.outreach_service is None:
            logger.warning(
                "OutreachCog._outreach_check: outreach_service not available — skipping cycle"
            )
            return

        ready_events = await self.ctx.outreach_service.run_gap_detection(guild_id)
        # Story 5.2: for each ready event, contact the next eligible DJ
        # Story 5.3: when no eligible DJs remain, check exhaustion and alert admin
        dm_attempts = 0
        exhaustion_checks = 0
        for event in ready_events:
            sent = await self.ctx.outreach_service.contact_next_dj(event, guild_id, self.ctx)
            if sent:
                dm_attempts += 1
            else:
                # No eligible DJs — check if outreach sequence should be marked exhausted
                await self.ctx.outreach_service.handle_exhaustion(event, guild_id)
                exhaustion_checks += 1
        logger.debug(
            "OutreachCog._outreach_check: %d event(s) ready, %d DM(s) attempted,"
            " %d exhaustion check(s)",
            len(ready_events),
            dm_attempts,
            exhaustion_checks,
        )
