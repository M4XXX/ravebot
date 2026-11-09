"""Setup cog: the /setup slash command that opens the guided setup wizard (admin only)."""
import logging
from typing import Any

import disnake
from disnake.ext import commands

from bot.context import BotContext
from bot.db.setup_drafts import SetupDraft, get_draft, save_draft
from bot.services import activation
from bot.ui import texts
from bot.ui.setup_wizard import (
    SetupWizardView,
    entry_view,
    guild_lock,
    migrate_legacy_draft,
    prefilled_data,
    render_step,
)

logger = logging.getLogger(__name__)

# Server only: Discord hides the command in private messages
SERVER_ONLY = disnake.InteractionContextTypes(guild=True)


class SetupCog(commands.Cog):
    """/setup works while the bot is idle (no activation gate here)."""

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    async def cog_slash_command_check(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        """Server only first, then the same admin whitelist as AdminCog."""
        if inter.guild_id is None:
            await inter.response.send_message(texts.ADMIN_SERVER_ONLY, ephemeral=True)
            return False
        return self.ctx.is_admin(inter.author.id)

    async def cog_slash_command_error(
        self, inter: disnake.ApplicationCommandInteraction, error: Exception
    ) -> None:
        if isinstance(error, commands.CheckFailure):
            if inter.response.is_done():
                return  # the check already answered (server only)
            await inter.response.send_message(texts.SETUP_NO_PERMISSION, ephemeral=True)
            return
        logger.error("Unexpected error in /setup: %s", error, exc_info=True)
        text = texts.GENERIC_ERROR
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except disnake.HTTPException:
            logger.warning("Could not send the /setup error reply", exc_info=True)

    async def _open_message(self, inter: disnake.ApplicationCommandInteraction, draft: SetupDraft):
        channel = inter.bot.get_channel(draft.channel_id)
        if channel is None:
            return None
        try:
            return await channel.fetch_message(draft.message_id)
        except disnake.HTTPException:
            return None

    @commands.slash_command(
        name="setup", description="Guided setup for the bot (admin only)", contexts=SERVER_ONLY
    )
    async def setup(self, inter: disnake.ApplicationCommandInteraction) -> None:
        db: Any = self.ctx.db
        guild_id = inter.guild_id
        if guild_id is None:
            await inter.response.send_message(texts.SETUP_NOT_IN_GUILD, ephemeral=True)
            return
        async with guild_lock(guild_id):
            draft = await get_draft(db, guild_id)
            if draft is not None:
                migrate_legacy_draft(draft)
                message = await self._open_message(inter, draft)
                if message is not None:
                    await inter.response.send_message(
                        texts.SETUP_ALREADY_OPEN.format(url=message.jump_url), ephemeral=True
                    )
                    return
                # The wizard message is gone: post it again at the same step
                await self._post(inter, draft.step, draft.data)
                return
            if await activation.is_active(db, guild_id):
                await inter.response.send_message(
                    texts.SETUP_ALREADY_ACTIVE, view=entry_view(self.ctx)
                )
                return
            data = await prefilled_data(db, guild_id, inter.guild, rerun=False)
            await self._post(inter, 0, data)

    async def _post(
        self, inter: disnake.ApplicationCommandInteraction, step: int, data: dict
    ) -> None:
        await inter.response.defer()
        assert inter.guild_id is not None
        draft = SetupDraft(inter.guild_id, 0, inter.channel_id, step, data)
        view = await render_step(self.ctx, inter.guild, draft)
        message = await inter.edit_original_message(
            content=view.content, view=SetupWizardView(self.ctx, view)
        )
        db: Any = self.ctx.db
        await save_draft(db, inter.guild_id, message.id, inter.channel_id, step, data)
