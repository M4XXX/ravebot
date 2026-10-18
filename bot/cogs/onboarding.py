"""Onboarding cog — DJ role detection and DM initiation.

Story 2.1: on_member_update — detects DJ role assignment, sends intro DM.
Story 2.2: Go Active button handler — opens onboarding modal, stores profile.
Story 2.3: Role assignment + Notion sync after modal submit.
Story 2.4: Pass for now handler + FallbackMenuView.

[Source: architecture.md#Cog Responsibility Boundary]
[Source: epics.md#FR1-FR8]
"""
import logging

import disnake
from disnake.ext import commands

from bot import active
from bot import guild as served
from bot.context import BotContext
from bot.db.djs import get_dj_by_discord_id
from bot.db.interventions import is_under_intervention
from bot.ui.embeds import ErrorCode
from bot.ui.views import (
    ONBOARDING_INTRO_TEXT,
    FallbackMenuView,
    OnboardingIntroView,
    StartOverView,
)

logger = logging.getLogger(__name__)

NOT_A_DJ_TEXT = (
    "Hi! I handle DJ bookings for this server. "
    "If you'd like to play, ask an organizer for the DJ role."
)


class OnboardingCog(commands.Cog):
    """Handles DJ role detection, DM onboarding flow, and FallbackMenuView."""

    def __init__(self, ctx: BotContext, bot: commands.Bot) -> None:
        self.ctx = ctx
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: disnake.Member,
        after: disnake.Member,
    ) -> None:
        """Detect DJ role assignment and initiate onboarding DM.

        Fires on any member attribute change. Filters to DJ role additions only.
        Checks DB before sending to prevent duplicate DMs.

        [Source: epics.md#Story 2.1 AC1-AC4]
        [Source: epics.md#FR1, FR2, FR8]
        """
        if not served.serves(self.bot, after.guild.id):
            return  # role change in a server this bot does not serve

        # Detect newly added roles only
        new_roles = set(after.roles) - set(before.roles)
        dj_role = next((r for r in new_roles if r.name.lower() == "dj"), None)
        if dj_role is None:
            return

        # Guard: pool may be None if bot is still starting up
        if self.ctx.db is None:
            logger.warning(
                "on_member_update: DB pool not available — skipping onboarding for user %s",
                after.id,
            )
            return

        if not await active.is_active(self.ctx.db, after.guild.id):
            logger.info("Passive mode: no onboarding DM for user %s", after.id)
            return

        # Check if DJ already exists in DB (any state — prevents duplicate DMs)
        existing = await get_dj_by_discord_id(self.ctx.db, after.id)
        if existing is not None:
            logger.debug(
                "on_member_update: DJ %s already in DB — skipping onboarding DM",
                after.id,
            )
            return

        await self.send_onboarding_dm(after)

    async def send_onboarding_dm(self, member: disnake.Member) -> str:
        """Send the intro DM with the Go Active / Pass buttons.

        Returns "sent" or "dms_closed". Callers decide whether the member should get it
        (new DJ role, /admin onboard, or a DJ without a profile who DMs the bot).
        """
        try:
            onboarding_text = ONBOARDING_INTRO_TEXT
            await member.send(
                onboarding_text,
                view=OnboardingIntroView(self.ctx, member.id, member.guild.id),
            )
            # Story 6.1 AC1: log outgoing onboarding DM
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_dm_transcript(
                    dj_name=member.name,
                    direction="BOT",
                    content=onboarding_text,
                )
            logger.info("Onboarding DM sent to %s (user_id=%s)", member.name, member.id)
            return "sent"
        except disnake.Forbidden:
            logger.warning(
                "Onboarding DM blocked for %s (user_id=%s) — DMs disabled",
                member.name,
                member.id,
            )
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_DM_FAILED,
                    trigger=(
                        f"Onboarding DM to {member.name} (user_id={member.id}) "
                        "was blocked — user has DMs from server members disabled."
                    ),
                    action_taken="Onboarding DM skipped. DJ was not added to the booking pool.",
                    manual_action=(
                        f"Ask {member.name} to enable DMs from server members "
                        f"(Discord Settings → Privacy & Safety), then re-assign the DJ role."
                    ),
                )
        return "dms_closed"

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        """Respond to free-text DMs with FallbackMenuView.

        Fires for every Discord message — filters to DMs from non-bot users only.
        Checks per-user lock: if held, user is in an active multi-step flow.
        FallbackMenuView NOT shown during active flows (AC5).

        [Source: epics.md#Story 2.4 AC2, AC5]
        [Source: ux-design-specification.md#UX-DR1]
        """
        # Only handle DMs
        if message.guild is not None:
            return
        # Ignore bot messages (including our own)
        if message.author.bot:
            return
        # Passive mode: DMs are neither answered nor mirrored to the log channel
        if not await active.is_active(self.ctx.db, served.resolve(self.bot)):
            return
        # Story 6.1 AC2: log all incoming DJ DMs regardless of flow state
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=message.author.name,
                direction="DJ",
                content=message.content,
            )

        # Story 6.2 AC3: if DJ is under admin intervention, forward to admin log and suppress
        if self.ctx.db is not None:
            if await is_under_intervention(self.ctx.db, message.author.id):
                if self.ctx.log_service is not None:
                    await self.ctx.log_service.post_intervention(
                        sender=f"@{message.author.name}",
                        recipient="ADMIN",
                        content=message.content,
                    )
                return

        # If user is in an active multi-step flow, do not interrupt
        lock = self.ctx.get_user_lock(message.author.id)
        if lock.locked():
            still_text = (
                "You're still in the middle of something with me — finish it, or start over:"
            )
            await message.channel.send(still_text, view=StartOverView(self.ctx))
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_dm_transcript(
                    dj_name=message.author.name, direction="BOT", content=still_text
                )
            return
        # Only registered DJs get the booking menu
        dj = await get_dj_by_discord_id(self.ctx.db, message.author.id) if self.ctx.db else None
        if dj is None or not dj["onboarding_complete"]:
            guild = served.served_guild(self.bot)
            member = guild.get_member(message.author.id) if guild is not None else None
            if member is not None and any(r.name.lower() == "dj" for r in member.roles):
                await self.send_onboarding_dm(member)
            else:
                await message.channel.send(NOT_A_DJ_TEXT)
                if self.ctx.log_service is not None:
                    await self.ctx.log_service.post_dm_transcript(
                        dj_name=message.author.name, direction="BOT", content=NOT_A_DJ_TEXT
                    )
            return

        fallback_text = "I work best with buttons — here's what I can do for you:"
        await message.channel.send(fallback_text, view=FallbackMenuView(self.ctx))
        # Story 6.1 AC1: log outgoing fallback menu DM
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=message.author.name,
                direction="BOT",
                content=fallback_text,
            )
