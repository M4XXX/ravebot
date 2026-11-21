"""Apply view: what to do with upcoming events after a rule or default change."""
import logging
from typing import Any

import disnake

from bot.services.schedule import PendingChange
from bot.ui import texts
from bot.ui.gates import refuse_in_maintenance

logger = logging.getLogger(__name__)

MAX_PICKS = 25
TIMEOUT_SECONDS = 15 * 60


class ApplyChangesView(disnake.ui.View):
    """Apply to all / Pick events / Keep all, for the admin who changed the rules."""

    def __init__(
        self, ctx: Any, guild_id: int, admin_id: int, changes: list[PendingChange]
    ) -> None:
        super().__init__(timeout=TIMEOUT_SECONDS)
        self._ctx = ctx
        self._guild_id = guild_id
        self._admin_id = admin_id
        self._changes = changes
        self._picked: list[str] = []
        self.shown = changes[:MAX_PICKS]
        # The interaction that opened the view, to edit the message on timeout
        self.origin: Any = None

    def lines(self) -> list[str]:
        return [texts.change_text(c.event, c.field, c.old, c.new) for c in self._changes]

    def content(self, intro: str | None = None) -> str:
        return texts.apply_content(self.lines(), intro)

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self._admin_id and self._ctx.is_admin(inter.author.id):
            return True
        await inter.response.send_message(texts.APPLY_NOT_YOURS, ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        if self.origin is None:
            return
        try:
            await self.origin.edit_original_message(content=texts.APPLY_EXPIRED, view=None)
        except Exception:
            logger.debug("apply view: could not edit the expired message", exc_info=True)

    async def _apply(self, inter: disnake.MessageInteraction, picks: list[tuple[str, str]] | None):
        self.stop()
        if await refuse_in_maintenance(inter, self._ctx):
            return
        await inter.response.defer()
        try:
            count = await self._ctx.schedule_service.apply(self._guild_id, picks)
            text = texts.apply_done(count)
        except Exception:
            logger.exception("apply view: apply failed")
            text = texts.GENERIC_ERROR
        await inter.edit_original_message(content=text, view=None)

    @disnake.ui.button(label=texts.APPLY_ALL, style=disnake.ButtonStyle.success)
    async def apply_all(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        await self._apply(inter, None)

    @disnake.ui.button(label=texts.APPLY_PICK, style=disnake.ButtonStyle.primary)
    async def pick_events(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        self.clear_items()
        options = [
            disnake.SelectOption(
                label=texts.option_label(texts.change_text(c.event, c.field, c.old, c.new)),
                value=f"{c.event.page_id}:{c.field}",
            )
            for c in self.shown
        ]
        select = disnake.ui.StringSelect(
            placeholder=texts.APPLY_PLACEHOLDER,
            min_values=1,
            max_values=len(options),
            options=options,
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)
        confirm = disnake.ui.Button(label=texts.APPLY_SELECTED, style=disnake.ButtonStyle.success)
        confirm.callback = self._on_confirm  # type: ignore[method-assign]
        self.add_item(confirm)
        note = f"\n{texts.APPLY_CAPPED}" if len(self._changes) > MAX_PICKS else ""
        await inter.response.edit_message(content=self.content() + note, view=self)

    async def _on_select(self, inter: disnake.MessageInteraction) -> None:
        self._picked = list(inter.values or [])
        await inter.response.defer()

    async def _on_confirm(self, inter: disnake.MessageInteraction) -> None:
        if not self._picked:
            await inter.response.send_message(texts.APPLY_NONE_PICKED, ephemeral=True)
            return
        picks = []
        for value in self._picked:
            uid, _, field = value.rpartition(":")
            picks.append((uid, field))
        await self._apply(inter, picks)

    @disnake.ui.button(label=texts.APPLY_KEEP, style=disnake.ButtonStyle.secondary)
    async def keep_all(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        self.stop()
        await inter.response.edit_message(content=texts.APPLY_KEPT, view=None)
