"""Idle gate shared by every DJ-facing view and the admin check."""
import logging

import disnake

from bot.services import activation

logger = logging.getLogger(__name__)

IDLE_REPLY = "I can't answer right now. Please try again later."
ADMIN_IDLE_REPLY = "The bot is not active yet. Run /setup to finish the setup."
STARTUP_ADMIN_DM = "RaveBot is not set up yet. Run /setup in the channel where you want to do it."


NOT_YOUR_MENU = "This menu belongs to someone else."


async def _reply(interaction: disnake.MessageInteraction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except disnake.HTTPException:
        pass


class GatedView(disnake.ui.View):
    """A view whose controls only work while the bot is active.

    While idle every interaction gets IDLE_REPLY privately and is blocked. The view
    finds the database through its `ctx` or `_ctx` attribute.
    """

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        # A per-user view (booking, cancelling, an invite) answers its owner only
        owner = getattr(self, "_user_id", None) or getattr(self, "_discord_user_id", None)
        author = getattr(getattr(interaction, "author", None), "id", None)
        if isinstance(owner, int) and isinstance(author, int) and author != owner:
            await _reply(interaction, NOT_YOUR_MENU)
            return False
        ctx = getattr(self, "ctx", None) or getattr(self, "_ctx", None)
        db = getattr(ctx, "db", None)
        bot = getattr(interaction, "bot", None) or getattr(interaction, "client", None)
        try:
            if await activation.bot_is_active(bot, db):
                return True
        except Exception:
            logger.warning("GatedView: could not read the activation state", exc_info=True)
        # Blocked: a flow held by this view ends here, so the user is not stuck with it
        release = getattr(self, "_release_flow", None)
        if callable(release):
            release()
        await _reply(interaction, IDLE_REPLY)
        return False
