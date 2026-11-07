"""Onboarding cog — DJ role detection and DM initiation.

Story 2.1: on_member_update — detects DJ role assignment, sends intro DM.
DJ onboarding rework: the intro buttons start the DM conversation (bot/ui/dj_profile.py).
Story 2.4: FallbackMenuView.

[Source: architecture.md#Cog Responsibility Boundary]
[Source: epics.md#FR1-FR8]
"""
import asyncio
import logging

import disnake
from disnake.ext import commands

from bot.context import BotContext
from bot.db.dj_conversations import get_conversation
from bot.db.djs import get_dj_by_discord_id
from bot.db.log_replies import save_log_reply
from bot.services import activation
from bot.services.roles import get_dj_role
from bot.ui import texts
from bot.ui.dj_profile import OnboardingIntroView, send_reply
from bot.ui.embeds import ErrorCode
from bot.ui.gates import IDLE_REPLY
from bot.ui.log_reply import LogReplyView
from bot.ui.views import FallbackMenuView

logger = logging.getLogger(__name__)


class OnboardingCog(commands.Cog):
    """Handles DJ role detection, DM onboarding flow, and FallbackMenuView."""

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx
        self._reply_view: LogReplyView | None = None
        self._intro_view: OnboardingIntroView | None = None
        self._tasks: set[asyncio.Task] = set()

    def _get_reply_view(self) -> LogReplyView:
        """One shared Reply view for every entry (disnake keeps each view it is given)."""
        if self._reply_view is None:
            self._reply_view = LogReplyView(self.ctx)
        return self._reply_view

    def _get_intro_view(self) -> OnboardingIntroView:
        """One shared intro view for every welcome DM (persistent, no per-DM growth)."""
        if self._intro_view is None:
            self._intro_view = OnboardingIntroView(self.ctx)
        return self._intro_view

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
        # Detect newly added roles only
        new_roles = set(after.roles) - set(before.roles)
        if not new_roles:
            return

        # Guard: pool may be None if bot is still starting up
        if self.ctx.db is None:
            logger.warning(
                "on_member_update: DB pool not available — skipping onboarding for user %s",
                after.id,
            )
            return

        configured_dj_role = await get_dj_role(self.ctx.db, after.guild)
        if configured_dj_role is None or configured_dj_role not in new_roles:
            return

        # Idle: the role assignment sends nothing
        if not await activation.bot_is_active(self.bot, self.ctx.db):
            logger.debug("on_member_update: bot is idle — no onboarding DM for %s", after.id)
            return

        # Check if DJ already exists in DB (any state — prevents duplicate DMs)
        existing = await get_dj_by_discord_id(self.ctx.db, after.id)
        if existing is not None:
            logger.debug(
                "on_member_update: DJ %s already in DB — skipping onboarding DM",
                after.id,
            )
            return

        # Attempt to send onboarding DM
        try:
            onboarding_text = texts.WELCOME
            await after.send(
                onboarding_text,
                view=self._get_intro_view(),
            )
            logger.info("Onboarding DM sent to %s (user_id=%s)", after.name, after.id)
        except disnake.Forbidden:
            logger.warning(
                "Onboarding DM blocked for %s (user_id=%s) — DMs disabled",
                after.name,
                after.id,
            )
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_DM_FAILED,
                    trigger=(
                        f"The welcome message to {after.display_name} could not be "
                        "delivered because their DMs are closed."
                    ),
                    action_taken="They have not been added to the DJ pool yet.",
                    manual_action=texts.dm_failed_todo(after.display_name),
                )

    async def _log_incoming(self, message: disnake.Message) -> None:
        """Post the typed DM (text and/or attachments) to the log channel. Never raises."""
        log_service = self.ctx.log_service
        content = message.content or ""
        attachments = list(message.attachments or [])
        if log_service is None or not (content or attachments):
            return
        try:
            dj_name = None
            if self.ctx.db is not None:
                try:
                    dj = await get_dj_by_discord_id(self.ctx.db, message.author.id)
                except Exception:
                    logger.exception("on_message: DJ lookup failed for %s", message.author.id)
                    dj = None
                shown = getattr(message.author, "display_name", None) or message.author.name
                if dj is not None and dj["dj_name"] != shown:
                    dj_name = dj["dj_name"]
            logged = await log_service.log_user_message(
                message.author,
                content,
                [a.filename for a in attachments],
                dj_name,
                self._get_reply_view(),
            )
            if logged is not None and self.ctx.db is not None:
                await save_log_reply(self.ctx.db, logged.id, message.author.id)
        except Exception:
            logger.exception("on_message: could not log DM from %s", message.author.id)

    async def _route_to_conversation(self, message: disnake.Message) -> bool:
        """Hand the DM to the DJ's open conversation. True when it was handled."""
        service = self.ctx.dj_conversations
        if service is None or self.ctx.db is None:
            return False
        user_id = message.author.id
        try:
            conversation = await get_conversation(self.ctx.db, user_id)
        except Exception:
            logger.exception("on_message: conversation lookup failed for %s", user_id)
            return False
        if conversation is None:
            return False
        try:
            reply = await service.handle_message(message)
        except Exception:
            logger.exception("on_message: DJ conversation failed for %s", user_id)
            await message.channel.send(texts.GENERIC_ERROR)
            return True
        if reply is None:
            # Conversation ended between lookup and handling: fall back to the menu
            return False
        await send_reply(self.ctx, message.channel, user_id, reply)
        return True

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

        # Idle: only the idle reply, nothing is logged and no menu is sent
        if not await activation.bot_is_active(self.bot, self.ctx.db):
            await message.channel.send(IDLE_REPLY)
            return

        # An open DJ conversation takes the message: no log entry, no menu
        if await self._route_to_conversation(message):
            return

        # Entry first, but in the background so the menu is not delayed by the log post
        task = asyncio.create_task(self._log_incoming(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        # If user is in an active multi-step flow, do not interrupt
        lock = self.ctx.get_user_lock(message.author.id)
        if lock.locked():
            return
        active: bool | None = None
        onboarded = False
        if self.ctx.db is not None:
            try:
                dj = await get_dj_by_discord_id(self.ctx.db, message.author.id)
            except Exception:
                logger.exception("on_message: DJ lookup failed for %s", message.author.id)
                dj = None
            if dj is not None:
                active = bool(dj["active_booking"])
                onboarded = True
        fallback_text = texts.MENU
        await message.channel.send(
            fallback_text, view=FallbackMenuView(self.ctx, active, onboarded)
        )
