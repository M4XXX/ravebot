"""Outreach cog — proactive outreach scheduler.

Story 1.5: DB unavailability guard pattern for scheduled task loop.
Full outreach implementation (FR25-30) in Story 5.x.

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: epics.md#Story 1.5 AC1]
"""
import logging

from disnake.ext import commands, tasks

from bot.cogs.loop_support import first_guild_id, guard_db, prepare_loop, run_cycle
from bot.context import BotContext
from bot.services import activation
from bot.services.outreach import outreach_enabled

logger = logging.getLogger(__name__)


class OutreachCog(commands.Cog):
    """Hosts the proactive outreach scheduler loop."""

    bot: commands.Bot  # injected by disnake when the cog is added

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    def cog_load(self) -> None:
        self._outreach_check.start()

    def cog_unload(self) -> None:
        self._outreach_check.cancel()

    async def _guard_db(self, operation: str) -> bool:
        """True if the DB answers (see loop_support.guard_db)."""
        return await guard_db(self.ctx, operation)

    def _get_guild_id(self) -> int | None:
        return first_guild_id(self.bot)

    @tasks.loop(minutes=30)
    async def _outreach_check(self) -> None:
        await run_cycle("outreach check", self._outreach_cycle)

    async def _outreach_cycle(self) -> None:
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
        if not await activation.is_active(self.ctx.db, guild_id):
            logger.debug("OutreachCog._outreach_check: bot is idle, skipping")
            return

        # Global switch, read fresh each cycle. Off: no gap detection, no invites,
        # no exhaustion handling. Nothing else in the bot depends on this.
        try:
            enabled = await outreach_enabled(self.ctx.db, guild_id)
        except Exception as exc:
            logger.warning("OutreachCog._outreach_check: reading the switch failed: %s", exc)
            return
        if not enabled:
            logger.debug("OutreachCog._outreach_check: automatic outreach is off, skipping")
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

    @_outreach_check.before_loop
    async def _before_outreach_check(self) -> None:
        await prepare_loop(self.bot, self.ctx.db)
