"""Guided setup wizard: persistent view, step rendering and the activation effects.

The wizard message carries no state. Every press reads the `setup_drafts` row found
by the message id, so it keeps working after a restart. Chosen values stay in the
draft until the last step writes them to the config table.

To add a step, insert its key into STEPS (before "summary"), add a renderer branch in
`render_step` and its controls to the view; navigation works on the list.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import disnake

from bot.db import config as db_config
from bot.db.setup_drafts import SetupDraft, delete_draft, get_draft, save_draft
from bot.services import activation
from bot.services.event_replenish import replenish_events
from bot.ui import texts

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)

# Ordered step keys. "summary" stays last; the numbered steps are all before it.
STEPS: list[str] = ["welcome", "dj_channel", "log_channel", "summary"]

# Event defaults written on activation, only when not stored yet.
EVENT_DEFAULTS: dict[str, str] = {
    "slots_per_event": "6",
    "default_start_time": "20:00",
    "slot_duration_minutes": "60",
    "event_interval_days": "7",
    "event_lookahead_count": "6",
}

CHANNEL_KEYS = ("dj_channel", "log_channel")

_REQUIRED_PERMISSIONS = (("send_messages", "Send Messages"), ("embed_links", "Embed Links"))

ID_BACK = "setup:back"
ID_NEXT = "setup:next"
ID_DJ_CHANNEL = "setup:dj_channel"
ID_LOG_CHANNEL = "setup:log_channel"
ID_ACTIVATE = "setup:activate"
ID_SAVE = "setup:save"
ID_CHANGE = "setup:change"

# One lock per guild: the activation effects run once even on concurrent presses.
_guild_locks: dict[int, asyncio.Lock] = {}


def _db(ctx: "BotContext") -> Any:
    return ctx.db


def guild_lock(guild_id: int) -> asyncio.Lock:
    lock = _guild_locks.get(guild_id)
    if lock is None:
        lock = _guild_locks[guild_id] = asyncio.Lock()
    return lock


def total_steps() -> int:
    """Number of numbered steps (the summary page is not numbered)."""
    return len(STEPS) - 1


def step_key(step: int) -> str:
    return STEPS[max(0, min(step, len(STEPS) - 1))]


def missing_permissions(channel: Any, me: Any) -> list[str]:
    perms = channel.permissions_for(me)
    return [label for attr, label in _REQUIRED_PERMISSIONS if not getattr(perms, attr, False)]


@dataclass
class StepView:
    """What a step shows: its text and which controls are visible or disabled."""

    content: str
    visible: set[str] = field(default_factory=set)
    next_disabled: bool = False
    next_label: str = "Next"


def _channel_check(guild: Any, channel_id: Any) -> tuple[bool, Any, str | None]:
    """(ok, channel, check text) for the stored channel id of a channel step."""
    if channel_id is None:
        return False, None, None
    channel = guild.get_channel(int(channel_id)) if guild is not None else None
    if channel is None:
        return False, None, texts.SETUP_CHANNEL_GONE
    missing = missing_permissions(channel, guild.me)  # type: ignore[union-attr]
    if missing:
        return False, channel, texts.setup_channel_missing(channel.name, missing)
    return True, channel, texts.setup_channel_ok(channel.name)


async def _welcome_checks(ctx: "BotContext") -> list[tuple[bool, str]]:
    db_ok = True
    try:
        await _db(ctx).fetchval("SELECT 1")
    except Exception:
        db_ok = False
    notion_ok = True
    try:
        await ctx.notion_client.list_events()  # type: ignore[union-attr]
    except Exception:
        notion_ok = False
    return [
        (True, "Discord connection"),
        (db_ok, "Database" if db_ok else "Database: I can't reach it"),
        (
            notion_ok,
            "Notion: Events, Slots and DJs found"
            if notion_ok
            else "Notion: I can't read the databases",
        ),
    ]


def summary_lines(data: dict[str, Any]) -> list[str]:
    def chan(key: str) -> str:
        value = data.get(key)
        return f"<#{value}>" if value else "not set"

    return [f"DJ channel: {chan('dj_channel')}", f"Log channel: {chan('log_channel')}"]


async def render_step(ctx: "BotContext", guild: Any, draft: SetupDraft) -> StepView:
    key = step_key(draft.step)
    total = total_steps()
    if key == "welcome":
        checks = await _welcome_checks(ctx)
        return StepView(
            texts.setup_welcome(total, checks),
            {ID_NEXT},
            next_disabled=not all(ok for ok, _ in checks),
            next_label="Start",
        )
    if key in CHANNEL_KEYS:
        ok, channel, check = _channel_check(guild, draft.data.get(key))
        is_dj = key == "dj_channel"
        content = texts.setup_channel_step(
            STEPS.index(key) + 1,
            total,
            "DJ channel" if is_dj else "Log channel",
            texts.SETUP_DJ_CHANNEL_QUESTION if is_dj else texts.SETUP_LOG_CHANNEL_QUESTION,
            f"<#{draft.data[key]}>" if draft.data.get(key) else None,
            check,
        )
        return StepView(
            content,
            {ID_BACK, ID_NEXT, ID_DJ_CHANNEL if is_dj else ID_LOG_CHANNEL},
            next_disabled=not ok,
        )
    rerun = bool(draft.data.get("rerun"))
    content = texts.setup_summary(summary_lines(draft.data), rerun)
    return StepView(content, {ID_BACK, ID_SAVE if rerun else ID_ACTIVATE})


class SetupWizardView(disnake.ui.View):
    """Persistent wizard controls. Built with a StepView it shows only that step's controls.

    Built without one (for `bot.add_view`) it holds every control so any press on an
    old wizard message is routed here after a restart.
    """

    def __init__(self, ctx: "BotContext", step: StepView | None = None) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        if step is None:
            return
        for item in list(self.children):
            cid = getattr(item, "custom_id", None)
            if cid not in step.visible:
                self.remove_item(item)
            elif cid == ID_NEXT:
                item.disabled = step.next_disabled  # type: ignore[union-attr]
                item.label = step.next_label  # type: ignore[union-attr]

    # --- access and state ---

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id in self.ctx.config.admin_user_ids:
            return True
        await _private(inter, texts.SETUP_ONLY_ORGANIZERS)
        return False

    async def _draft(self, inter: disnake.MessageInteraction) -> SetupDraft | None:
        draft = await get_draft(_db(self.ctx), inter.guild_id)  # type: ignore[arg-type]
        if draft is None or draft.message_id != inter.message.id:
            return None
        return draft

    async def _show(self, inter: disnake.MessageInteraction, draft: SetupDraft) -> None:
        """Persist the draft and edit the wizard message to the draft's step."""
        await save_draft(
            _db(self.ctx),
            draft.guild_id,
            draft.message_id,
            draft.channel_id,
            draft.step,
            draft.data,
        )
        view = await render_step(self.ctx, inter.guild, draft)
        await inter.edit_original_message(
            content=view.content, view=SetupWizardView(self.ctx, view)
        )

    async def _navigate(self, inter: disnake.MessageInteraction, delta: int) -> None:
        await inter.response.defer()
        draft = await self._draft(inter)
        if draft is None:
            await _private(inter, texts.SETUP_ALREADY_DONE)
            return
        if delta > 0:
            view = await render_step(self.ctx, inter.guild, draft)
            if view.next_disabled:
                await self._show(inter, draft)  # refresh the checks, stay on this step
                return
        draft.step = max(0, min(draft.step + delta, len(STEPS) - 1))
        await self._show(inter, draft)

    # --- controls ---

    @disnake.ui.button(label="Back", style=disnake.ButtonStyle.secondary, custom_id=ID_BACK)
    async def back(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await self._navigate(inter, -1)

    @disnake.ui.button(label="Next", style=disnake.ButtonStyle.primary, custom_id=ID_NEXT)
    async def next(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await self._navigate(inter, 1)

    async def _choose_channel(
        self, inter: disnake.MessageInteraction, key: str
    ) -> None:
        await inter.response.defer()
        draft = await self._draft(inter)
        if draft is None:
            await _private(inter, texts.SETUP_ALREADY_DONE)
            return
        chosen = (inter.values or [None])[0]
        draft.data[key] = int(getattr(chosen, "id", chosen))
        await self._show(inter, draft)

    @disnake.ui.channel_select(
        custom_id=ID_DJ_CHANNEL,
        placeholder="Select a channel",
        channel_types=[disnake.ChannelType.text],
    )
    async def dj_channel(
        self, select: disnake.ui.ChannelSelect, inter: disnake.MessageInteraction
    ) -> None:
        await self._choose_channel(inter, "dj_channel")

    @disnake.ui.channel_select(
        custom_id=ID_LOG_CHANNEL,
        placeholder="Select a channel",
        channel_types=[disnake.ChannelType.text],
    )
    async def log_channel(
        self, select: disnake.ui.ChannelSelect, inter: disnake.MessageInteraction
    ) -> None:
        await self._choose_channel(inter, "log_channel")

    @disnake.ui.button(label="Activate", style=disnake.ButtonStyle.success, custom_id=ID_ACTIVATE)
    async def activate(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        await inter.response.defer()
        await activate_from_draft(self.ctx, inter)

    @disnake.ui.button(label="Save", style=disnake.ButtonStyle.success, custom_id=ID_SAVE)
    async def save(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.defer()
        await save_from_draft(self.ctx, inter)

    @disnake.ui.button(
        label=texts.SETUP_CHANGE_BUTTON, style=disnake.ButtonStyle.primary, custom_id=ID_CHANGE
    )
    async def change(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.defer()
        async with guild_lock(inter.guild_id):  # type: ignore[arg-type]
            existing = await get_draft(_db(self.ctx), inter.guild_id)  # type: ignore[arg-type]
            if existing is not None and existing.message_id != inter.message.id:
                await _private(
                    inter, texts.SETUP_ALREADY_OPEN.format(url=_jump_url(inter, existing))
                )
                return
            draft = existing or SetupDraft(
                inter.guild_id,  # type: ignore[arg-type]
                inter.message.id,
                inter.channel_id,
                0,
                await prefilled_data(_db(self.ctx), inter.guild_id),  # type: ignore[arg-type]
            )
            await self._show(inter, draft)


def entry_view(ctx: "BotContext") -> SetupWizardView:
    """The view of the "already active" message: only the Change settings button."""
    return SetupWizardView(ctx, StepView("", {ID_CHANGE}))


def _jump_url(inter: Any, draft: SetupDraft) -> str:
    return f"https://discord.com/channels/{inter.guild_id}/{draft.channel_id}/{draft.message_id}"


async def _private(inter: Any, text: str) -> None:
    try:
        if inter.response.is_done():
            await inter.followup.send(text, ephemeral=True)
        else:
            await inter.response.send_message(text, ephemeral=True)
    except disnake.HTTPException:
        logger.warning("Setup wizard: could not send a private reply", exc_info=True)


async def prefilled_data(db: Any, guild_id: int) -> dict[str, Any]:
    """Draft data of a re-run: the stored values, marked as a re-run."""
    data: dict[str, Any] = {"rerun": True}
    for key in CHANNEL_KEYS:
        raw = await db_config.get_value(db, guild_id, key)
        if raw is not None:
            try:
                data[key] = int(raw)
            except ValueError:
                pass
    return data


async def _write_channels(db: Any, guild_id: int, data: dict[str, Any]) -> bool:
    if any(data.get(key) is None for key in CHANNEL_KEYS):
        return False
    for key in CHANNEL_KEYS:
        await db_config.set_value(db, guild_id, key, str(data[key]))
    return True


async def activate_from_draft(ctx: "BotContext", inter: Any) -> None:
    """First activation: write the keys, set the flag, run the effects exactly once."""
    guild_id = inter.guild_id
    db = _db(ctx)
    async with guild_lock(guild_id):
        draft = await get_draft(db, guild_id)
        if draft is None or draft.message_id != inter.message.id or draft.data.get("rerun"):
            await _private(inter, texts.SETUP_ALREADY_DONE)
            return
        if any(draft.data.get(key) is None for key in CHANNEL_KEYS):
            await _private(inter, texts.SETUP_MISSING_VALUES)
            return
        try:
            await _write_channels(db, guild_id, draft.data)
            for key, default in EVENT_DEFAULTS.items():
                if await db_config.get_value(db, guild_id, key) is None:
                    await db_config.set_value(db, guild_id, key, default)
            await activation.set_active(db, guild_id)
            created = await _run_effects(ctx, guild_id)
            await delete_draft(db, guild_id)
        except Exception:
            logger.exception("Setup wizard: activation failed")
            await _private(inter, texts.SETUP_ERROR)
            return
        await inter.edit_original_message(
            content=texts.setup_active_message(created, f"<#{draft.data['dj_channel']}>"),
            view=None,
        )


async def _run_effects(ctx: "BotContext", guild_id: int) -> int:
    """Create the missing future events and post the first Lineup Update."""
    created = 0
    if ctx.notion_client is not None:
        events = await ctx.notion_client.list_events()
        created = await replenish_events(_db(ctx), ctx.notion_client, guild_id, events)
        if ctx.lineup_service is not None:
            fresh = await ctx.notion_client.list_events() if created else events
            await ctx.lineup_service.refresh(fresh)
    return created


async def save_from_draft(ctx: "BotContext", inter: Any) -> None:
    """Re-run: store the changed values; no events are created and nothing is reposted."""
    guild_id = inter.guild_id
    async with guild_lock(guild_id):
        draft = await get_draft(_db(ctx), guild_id)
        if draft is None or draft.message_id != inter.message.id:
            await _private(inter, texts.SETUP_ALREADY_DONE)
            return
        if not await _write_channels(_db(ctx), guild_id, draft.data):
            await _private(inter, texts.SETUP_MISSING_VALUES)
            return
        await delete_draft(_db(ctx), guild_id)
        await inter.edit_original_message(content=texts.SETUP_SAVED, view=None)
