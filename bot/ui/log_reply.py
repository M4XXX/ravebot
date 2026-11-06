"""Reply button on "Message from ..." log entries.

An organizer taps Reply, types an answer in a popup and the bot sends it to the
user as a DM. The log message id maps to the user in the log_replies table, so
the button keeps working after restarts (persistent view, timeout=None).
"""
import logging
import re
from typing import TYPE_CHECKING

import disnake

from bot.db.log_replies import get_log_reply_user
from bot.services.dm import dm_user
from bot.ui import texts
from bot.ui.embeds import _safe_dj_name

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)

_TITLE_PREFIX = "Message from "
_FIELD_MAX = 1024
_CUT_NOTE = " (cut)"


def _reply_field_value(text: str) -> str:
    """Escape the reply for the entry field; cut the raw text first, note the cut."""
    escaped = texts.escape_text(text)
    if len(escaped) <= _FIELD_MAX:
        return escaped
    keep = _FIELD_MAX - len(_CUT_NOTE)
    while keep > 0:
        value = texts.escape_text(text[:keep]) + _CUT_NOTE
        if len(value) <= _FIELD_MAX:
            return value
        keep -= max(1, (len(value) - _FIELD_MAX) // 2)
    return _CUT_NOTE.strip()


class ReplyModal(disnake.ui.Modal):
    """Popup with one paragraph field; submit DMs the text to the user."""

    def __init__(
        self,
        ctx: "BotContext",
        user_id: int,
        name: str,
        log_message: disnake.Message,
    ) -> None:
        self._ctx = ctx
        self.user_id = user_id
        self._log_message = log_message
        super().__init__(
            title=f"Reply to {name}"[:45],
            custom_id=f"log_reply_modal:{log_message.id}",
            components=[
                disnake.ui.TextInput(
                    label="Message",
                    custom_id="message",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=2000,
                    required=True,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        text = inter.text_values["message"]
        await inter.response.defer(ephemeral=True)
        if not await dm_user(inter.bot, self.user_id, text):
            await inter.followup.send(texts.LOG_REPLY_FAILED, ephemeral=True)
            return
        try:
            organizer = _safe_dj_name(
                getattr(inter.author, "display_name", None) or inter.author.name
            )
            entry = self._log_message
            try:
                # inter.message is stale: another organizer may have replied meanwhile
                entry = await entry.channel.fetch_message(entry.id)
            except Exception:
                entry = self._log_message
            embed = disnake.Embed.from_dict(entry.embeds[0].to_dict())
            embed.add_field(
                name=f"{organizer} replied",
                value=_reply_field_value(text),
                inline=False,
            )
            await entry.edit(embed=embed, view=None)
        except Exception:
            logger.exception("Reply sent but the log entry could not be updated")
        await inter.followup.send(texts.LOG_REPLY_SENT, ephemeral=True)


class LogReplyView(disnake.ui.View):
    """One persistent Reply button shared by all message entries."""

    def __init__(self, ctx: "BotContext") -> None:
        super().__init__(timeout=None)
        self.ctx = ctx

    @disnake.ui.button(label="Reply", style=disnake.ButtonStyle.primary, custom_id="log:reply")
    async def reply(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        if inter.author.id not in self.ctx.config.admin_user_ids:
            await inter.response.send_message(texts.LOG_REPLY_ORGANIZERS_ONLY, ephemeral=True)
            return
        user_id = None
        if self.ctx.db is not None:
            try:
                user_id = await get_log_reply_user(self.ctx.db, inter.message.id)
            except Exception:
                logger.exception("Reply: mapping lookup failed")
        if user_id is None:
            await inter.response.send_message(texts.LOG_REPLY_NO_USER, ephemeral=True)
            return
        title = inter.message.embeds[0].title if inter.message.embeds else None
        name = (title or "").removeprefix(_TITLE_PREFIX).split(" (DJ ")[0]
        name = re.sub(r"\\(.)", r"\1", name).replace("\u200b", "") or "user"
        await inter.response.send_modal(ReplyModal(self.ctx, user_id, name, inter.message))
