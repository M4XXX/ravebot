"""The default DM menu: sent again a moment after a flow ends, replacing the old one."""
import asyncio
import logging
from typing import TYPE_CHECKING

import disnake

from bot.db import dm_menus
from bot.db.djs import get_dj_by_discord_id
from bot.services import activation
from bot.ui import texts

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)

MENU_DELAY_SECONDS = 2.0


class MenuService:
    """Schedules and sends the DM menu. Never raises: a failed menu is only logged."""

    def __init__(self, bot: object, ctx: "BotContext", delay: float = MENU_DELAY_SECONDS) -> None:
        self.bot = bot
        self.ctx = ctx
        self.delay = delay
        self._pending: dict[int, asyncio.Task] = {}

    def schedule(self, user_id: int) -> None:
        """Send the menu to the user after the delay. A newer call replaces a pending one."""
        if user_id <= 0:
            return  # a guest DJ has no Discord account
        self._cancel(user_id)
        try:
            task = asyncio.get_running_loop().create_task(self._run(user_id))
        except RuntimeError:
            return  # no running loop (not inside the bot)
        self._pending[user_id] = task

    def _cancel(self, user_id: int) -> None:
        task = self._pending.pop(user_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _run(self, user_id: int) -> None:
        try:
            await asyncio.sleep(self.delay)
            if self._pending.get(user_id) is asyncio.current_task():
                del self._pending[user_id]
            if await self._should_skip(user_id):
                return
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            channel = await user.create_dm()
            await self.post_now(channel, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("MenuService: could not send the menu to %s", user_id, exc_info=True)

    async def _should_skip(self, user_id: int) -> bool:
        if self.ctx.get_user_lock(user_id).locked():
            return True  # a new flow started in the meantime
        service = self.ctx.dj_conversations
        if service is not None and await service.has_open_conversation(user_id):
            return True
        return not await activation.bot_is_active(self.bot, self.ctx.db)

    async def post_now(self, channel: disnake.abc.Messageable, user_id: int) -> None:
        """Send the menu now: delete the old menu of this user, send, remember the new one."""
        from bot.ui.views import FallbackMenuView

        self._cancel(user_id)
        db = self.ctx.db
        active: bool | None = None
        onboarded = False
        if db is not None:
            try:
                dj = await get_dj_by_discord_id(db, user_id)
            except Exception:
                logger.exception("MenuService: DJ lookup failed for %s", user_id)
                dj = None
            if dj is not None:
                active = bool(dj["active_booking"])
                onboarded = True
        old = None
        if db is not None:
            try:
                old = await dm_menus.get_menu(db, user_id)
            except Exception:
                logger.warning("MenuService: menu lookup failed for %s", user_id, exc_info=True)
        sent = await channel.send(texts.MENU, view=FallbackMenuView(self.ctx, active, onboarded))
        if db is not None:
            try:
                await dm_menus.set_menu(db, user_id, sent.channel.id, sent.id)
            except Exception:
                logger.warning(
                    "MenuService: could not store the menu of %s", user_id, exc_info=True
                )
                return  # keep the old record's message undeleted: the next menu removes it
        if old is not None and old.message_id != sent.id:
            await self._delete(channel, old.message_id)

    async def _delete(self, channel: disnake.abc.Messageable, message_id: int) -> None:
        try:
            await channel.get_partial_message(message_id).delete()
        except disnake.NotFound:
            logger.debug("MenuService: the old menu was already gone")
        except Exception:
            logger.warning("MenuService: could not delete the old menu", exc_info=True)
