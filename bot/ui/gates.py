"""Idle gate shared by every DJ-facing view and the admin check."""
import logging

import disnake

from bot.services import activation

logger = logging.getLogger(__name__)

IDLE_REPLY = "I can't answer right now. Please try again later."
ADMIN_IDLE_REPLY = "The bot is not active yet. Run /setup to finish the setup."
STARTUP_ADMIN_DM = "RaveBot is not set up yet. Run /setup in the channel where you want to do it."


class GatedView(disnake.ui.View):
    """A view whose controls only work while the bot is active.

    While idle every interaction gets IDLE_REPLY privately and is blocked. The view
    finds the database through its `ctx` or `_ctx` attribute.
    """

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        ctx = getattr(self, "ctx", None) or getattr(self, "_ctx", None)
        db = getattr(ctx, "db", None)
        bot = getattr(interaction, "bot", None) or getattr(interaction, "client", None)
        try:
            if await activation.bot_is_active(bot, db):
                return True
        except Exception:
            logger.warning("GatedView: could not read the activation state", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(IDLE_REPLY, ephemeral=True)
            else:
                await interaction.response.send_message(IDLE_REPLY, ephemeral=True)
        except disnake.HTTPException:
            pass
        return False
