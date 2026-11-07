"""Guided setup wizard: persistent view, step rendering and the activation effects.

The wizard message carries no state. Every press reads the `setup_drafts` row found
by the message id, so it keeps working after a restart. Chosen values stay in the
draft until the last step writes them to the config table.

To add a step, insert its key into STEPS (before "summary"), add a renderer branch in
`render_step` and its controls to the view; navigation works on the list.
"""
import asyncio
import datetime
import logging
import re
import zoneinfo
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import disnake

from bot.db import config as db_config
from bot.db.setup_drafts import SetupDraft, delete_draft, get_draft, save_draft
from bot.notion.models import MAX_SLOTS
from bot.services import activation
from bot.services.event_replenish import replenish_events
from bot.services.outreach import parse_outreach_enabled
from bot.timezone import DEFAULT_TIMEZONE, is_valid_timezone, set_timezone
from bot.ui import texts

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)

# Ordered step keys. "summary" stays last; the numbered steps are all before it
# (the summary belongs to the last numbered step).
STEPS: list[str] = [
    "welcome",
    "dj_channel",
    "log_channel",
    "roles",
    "events",
    "invites",
    "summary",
]

# Values a draft starts with (first run); a re-run starts from the stored values.
DEFAULT_VALUES: dict[str, Any] = {
    "slots_per_event": 6,
    "default_start_time": "20:00",
    "slot_duration_minutes": 60,
    "event_interval_days": 7,
    "event_lookahead_count": 6,
    "timezone": DEFAULT_TIMEZONE,
    "outreach_enabled": True,
    "outreach_days_before": 14,
    "outreach_cooldown_hours": 12,
}
_INT_VALUE_KEYS = (
    "slots_per_event",
    "slot_duration_minutes",
    "event_interval_days",
    "event_lookahead_count",
    "outreach_days_before",
    "outreach_cooldown_hours",
)
MAX_SLOT_MINUTES = 1440
MAX_INTERVAL_DAYS = 365
MAX_EVENTS_AHEAD = 52
MAX_INVITE_DAYS = 365
MAX_INVITE_HOURS = 720
CHANNEL_KEYS = ("dj_channel", "log_channel")
ROLE_KEYS = ("dj_role_id", "invites_role_id")

_REQUIRED_PERMISSIONS = (("send_messages", "Send Messages"), ("embed_links", "Embed Links"))

ID_BACK = "setup:back"
ID_NEXT = "setup:next"
ID_DJ_CHANNEL = "setup:dj_channel"
ID_LOG_CHANNEL = "setup:log_channel"
ID_DJ_ROLE = "setup:dj_role"
ID_INVITES_ROLE = "setup:invites_role"
ID_CREATE_ROLE = "setup:create_role"
ID_EVENTS_CHANGE = "setup:events_change"
ID_ZONE = "setup:zone"
ID_INVITES_TOGGLE = "setup:invites_toggle"
ID_INVITES_CHANGE = "setup:invites_change"
ID_ACTIVATE = "setup:activate"
ID_SAVE = "setup:save"
ID_CHANGE = "setup:change"

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# One lock per guild: the activation effects run once even on concurrent presses.
_guild_locks: dict[int, asyncio.Lock] = {}


def _refusal(draft: SetupDraft | None, message_id: int) -> str | None:
    """Private note when the pressed message is not the open wizard, else None."""
    if draft is None:
        return texts.SETUP_ALREADY_DONE
    if draft.message_id != message_id:
        return texts.SETUP_OUTDATED
    return None


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


def step_number(key: str) -> int:
    return STEPS.index(key) + 1


def zone_abbr(name: str) -> str:
    """Current abbreviation of the zone (CET or CEST for Europe/Berlin), else its name."""
    try:
        return datetime.datetime.now(zoneinfo.ZoneInfo(name)).tzname() or name
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return name


def values(data: dict[str, Any]) -> dict[str, Any]:
    """The draft's values with the defaults filled in."""
    merged = dict(DEFAULT_VALUES)
    merged.update({k: data[k] for k in DEFAULT_VALUES if k in data})
    return merged


# --- validation of the popup fields ---

def parse_hhmm(text: str) -> str | None:
    """`H:MM` or `HH:MM` from 00:00 to 23:59 as zero-padded `HH:MM`, else None."""
    match = _HHMM_RE.match((text or "").strip())
    if match and int(match.group(1)) <= 23 and int(match.group(2)) <= 59:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def parse_events_fields(raw: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Validate the five event fields; errors name the field."""
    out: dict[str, Any] = {}
    errors: list[str] = []

    def number(field: str, label: str, key: str, maximum: int) -> None:
        rule = f"enter a whole number from 1 to {maximum}."
        try:
            value = int((raw.get(field) or "").strip())
        except ValueError:
            errors.append(texts.setup_invalid(label, rule))
            return
        if value < 1 or value > maximum:
            errors.append(texts.setup_invalid(label, rule))
            return
        out[key] = value

    number("slots", "Slots per event", "slots_per_event", MAX_SLOTS)
    number("length", "Slot length", "slot_duration_minutes", MAX_SLOT_MINUTES)
    number("interval", "One event every (days)", "event_interval_days", MAX_INTERVAL_DAYS)
    number("ahead", "Future events kept ready", "event_lookahead_count", MAX_EVENTS_AHEAD)
    start = parse_hhmm(raw.get("start") or "")
    if start is not None:
        out["default_start_time"] = start
    else:
        errors.append(
            texts.setup_invalid("Start time (HH:MM)", "enter a time from 00:00 to 23:59.")
        )
    return out, errors


def parse_invites_fields(raw: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    errors: list[str] = []
    for fid, label, key, maximum in (
        ("days", "Days before an event", "outreach_days_before", MAX_INVITE_DAYS),
        ("hours", "Hours between invites", "outreach_cooldown_hours", MAX_INVITE_HOURS),
    ):
        try:
            value = int((raw.get(fid) or "").strip())
        except ValueError:
            value = 0
        if value < 1 or value > maximum:
            errors.append(
                texts.setup_invalid(label, f"enter a whole number from 1 to {maximum}.")
            )
        else:
            out[key] = value
    return out, errors


def parse_zone_field(raw: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    name = (raw.get("zone") or "").strip()
    if not name or not is_valid_timezone(name):
        return {}, [
            texts.setup_invalid("Time zone", "enter a name like Europe/Berlin.")
        ]
    return {"timezone": name}, []


# --- checks ---

def missing_permissions(channel: Any, me: Any) -> list[str]:
    perms = channel.permissions_for(me)
    return [label for attr, label in _REQUIRED_PERMISSIONS if not getattr(perms, attr, False)]


@dataclass
class StepView:
    """What a step shows: its text, its controls in order and their state."""

    content: str
    visible: list[str] = field(default_factory=list)
    next_disabled: bool = False
    next_label: str = "Next"
    toggle_label: str = "Turn off"
    defaults: dict[str, int] = field(default_factory=dict)  # select id -> stored choice


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


def _role_check(guild: Any, data: dict[str, Any]) -> tuple[bool, str | None]:
    """(ok, check text): two different, manageable roles chosen."""
    chosen = [data[key] for key in ROLE_KEYS if data.get(key) is not None]
    if len(chosen) < len(ROLE_KEYS):
        return False, None
    if chosen[0] == chosen[1]:
        return False, texts.SETUP_ROLES_SAME
    me = guild.me
    if not me.guild_permissions.manage_roles:
        return False, texts.SETUP_ROLE_MISSING_PERMISSION
    for role_id in chosen:
        role = guild.get_role(int(role_id))
        if role is None:
            return False, texts.SETUP_ROLE_GONE
        if int(role_id) == guild.id:
            return False, texts.SETUP_ROLE_EVERYONE
        if getattr(role, "managed", False):
            return False, texts.setup_role_managed(role.name)
        if role.position >= me.top_role.position:
            return False, texts.setup_role_too_high(role.name)
    return True, texts.SETUP_ROLES_OK


def _role_name(guild: Any, role_id: Any) -> str | None:
    if role_id is None:
        return None
    role = guild.get_role(int(role_id)) if guild is not None else None
    return f"@{role.name}" if role is not None else "a role that no longer exists"


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


async def summary_lines(ctx: "BotContext", guild: Any, data: dict[str, Any]) -> list[str]:
    def chan(key: str) -> str:
        value = data.get(key)
        return f"<#{value}>" if value else "not set"

    v = values(data)
    dj = _role_name(guild, data.get("dj_role_id")) or "not set"
    inv = _role_name(guild, data.get("invites_role_id")) or "not set"
    invites = (
        f"on, {v['outreach_days_before']} days before, {v['outreach_cooldown_hours']} h wait"
        if v["outreach_enabled"]
        else "off"
    )
    lines = [
        f"DJ channel: {chan('dj_channel')}",
        f"Log channel: {chan('log_channel')}",
        f"DJ role: {dj} \u00b7 Invites role: {inv}",
        f"Events: {v['slots_per_event']} slots, "
        f"{v['default_start_time']} {zone_abbr(v['timezone'])}, "
        f"{v['slot_duration_minutes']} min, every {v['event_interval_days']} days, "
        f"{v['event_lookahead_count']} ahead",
        f"Time zone: {v['timezone']}",
        f"Automatic invites: {invites}",
    ]
    previous_zone = data.get("orig_timezone", DEFAULT_TIMEZONE)
    if previous_zone != v["timezone"]:
        lines.append(texts.SETUP_ZONE_CHANGED_WARNING)
    previous_role = data.get("orig_invites_role_id")
    chosen_role = data.get("invites_role_id")
    if previous_role is not None and chosen_role is not None and previous_role != chosen_role:
        lines.append(texts.SETUP_INVITES_ROLE_WARNING)
    return lines


async def render_step(ctx: "BotContext", guild: Any, draft: SetupDraft) -> StepView:
    key = step_key(draft.step)
    total = total_steps()
    data = draft.data
    if key == "welcome":
        checks = await _welcome_checks(ctx)
        return StepView(
            texts.setup_welcome(total, checks, bool(data.get("rerun"))),
            [ID_NEXT],
            next_disabled=not all(ok for ok, _ in checks),
            next_label="Start",
        )
    if key in CHANNEL_KEYS:
        ok, channel, check = _channel_check(guild, data.get(key))
        is_dj = key == "dj_channel"
        content = texts.setup_channel_step(
            step_number(key),
            total,
            "DJ channel" if is_dj else "Log channel",
            texts.SETUP_DJ_CHANNEL_QUESTION if is_dj else texts.SETUP_LOG_CHANNEL_QUESTION,
            f"<#{data[key]}>" if data.get(key) else None,
            check,
        )
        select_id = ID_DJ_CHANNEL if is_dj else ID_LOG_CHANNEL
        return StepView(
            content,
            [select_id, ID_BACK, ID_NEXT],
            next_disabled=not ok,
            defaults={select_id: int(data[key])} if data.get(key) else {},
        )
    if key == "roles":
        ok, check = _role_check(guild, data)
        content = texts.setup_roles_step(
            step_number(key),
            total,
            _role_name(guild, data.get("dj_role_id")),
            _role_name(guild, data.get("invites_role_id")),
            check,
        )
        defaults = {}
        if data.get("dj_role_id"):
            defaults[ID_DJ_ROLE] = int(data["dj_role_id"])
        if data.get("invites_role_id"):
            defaults[ID_INVITES_ROLE] = int(data["invites_role_id"])
        return StepView(
            content,
            [ID_DJ_ROLE, ID_INVITES_ROLE, ID_CREATE_ROLE, ID_BACK, ID_NEXT],
            next_disabled=not ok,
            defaults=defaults,
        )
    if key == "events":
        return StepView(
            texts.setup_events_step(
                step_number(key),
                total,
                {**values(data), "zone_abbr": zone_abbr(values(data)["timezone"])},
            ),
            [ID_EVENTS_CHANGE, ID_ZONE, ID_BACK, ID_NEXT],
        )
    if key == "invites":
        on = values(data)["outreach_enabled"]
        return StepView(
            texts.setup_invites_step(step_number(key), total, values(data)),
            [ID_INVITES_TOGGLE, ID_INVITES_CHANGE, ID_BACK, ID_NEXT],
            toggle_label="Turn off" if on else "Turn on",
        )
    rerun = bool(data.get("rerun"))
    content = texts.setup_summary(await summary_lines(ctx, guild, data), rerun, total)
    return StepView(content, [ID_BACK, ID_SAVE if rerun else ID_ACTIVATE])


Handler = Callable[[Any], Awaitable[None]]


class SetupWizardView(disnake.ui.View):
    """Persistent wizard controls for one step (all callbacks are stateless).

    Built with a StepView it shows that step's controls. Built without one it has no
    controls: it only serves its handlers and the access check. `registered_views`
    gives one view per step for `bot.add_view`, so every control id is routed here
    after a restart.
    """

    def __init__(self, ctx: "BotContext", step: StepView | None = None) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        for cid in step.visible if step else []:
            self.add_item(self._make_item(cid, step))  # type: ignore[arg-type]

    def _make_item(self, cid: str, step: StepView) -> disnake.ui.Item:
        buttons: dict[str, tuple[str, disnake.ButtonStyle, Handler]] = {
            ID_BACK: ("Back", disnake.ButtonStyle.secondary, self.on_back),
            ID_NEXT: (step.next_label, disnake.ButtonStyle.primary, self.on_next),
            ID_CREATE_ROLE: (
                "Create it for me",
                disnake.ButtonStyle.secondary,
                self.on_create_role,
            ),
            ID_EVENTS_CHANGE: ("Change", disnake.ButtonStyle.secondary, self.on_events_change),
            ID_ZONE: ("Time zone", disnake.ButtonStyle.secondary, self.on_zone),
            ID_INVITES_TOGGLE: (step.toggle_label, disnake.ButtonStyle.secondary, self.on_toggle),
            ID_INVITES_CHANGE: ("Change", disnake.ButtonStyle.secondary, self.on_invites_change),
            ID_ACTIVATE: ("Activate", disnake.ButtonStyle.success, self.on_activate),
            ID_SAVE: ("Save", disnake.ButtonStyle.success, self.on_save),
            ID_CHANGE: (texts.SETUP_CHANGE_BUTTON, disnake.ButtonStyle.primary, self.on_change),
        }
        if cid in buttons:
            label, style, handler = buttons[cid]
            item: disnake.ui.Item = disnake.ui.Button(label=label, style=style, custom_id=cid)
            if cid == ID_NEXT:
                item.disabled = step.next_disabled  # type: ignore[attr-defined]
            item.callback = handler  # type: ignore[method-assign,assignment]
            return item
        default = step.defaults.get(cid)
        defaults = [disnake.Object(id=default)] if default else None
        if cid in (ID_DJ_CHANNEL, ID_LOG_CHANNEL):
            key = "dj_channel" if cid == ID_DJ_CHANNEL else "log_channel"
            item = disnake.ui.ChannelSelect(
                custom_id=cid,
                placeholder="Select a channel",
                channel_types=[disnake.ChannelType.text],
                default_values=defaults,  # type: ignore[arg-type]
            )
            item.callback = self._select_handler(self._choose_channel, key)  # type: ignore[method-assign,assignment]
            return item
        key = "dj_role_id" if cid == ID_DJ_ROLE else "invites_role_id"
        item = disnake.ui.RoleSelect(
            custom_id=cid,
            placeholder="Select the DJ role" if cid == ID_DJ_ROLE else "Select the invites role",
            default_values=defaults,  # type: ignore[arg-type]
        )
        item.callback = self._select_handler(self._choose_role, key)  # type: ignore[method-assign,assignment]
        return item

    @staticmethod
    def _select_handler(
        method: Callable[[Any, str], Awaitable[None]], key: str
    ) -> Handler:
        async def handler(inter: Any) -> None:
            await method(inter, key)

        return handler

    # --- access and state ---

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id in self.ctx.config.admin_user_ids:
            return True
        await _private(inter, texts.SETUP_ONLY_ORGANIZERS)
        return False

    async def _draft(self, inter: Any) -> SetupDraft | None:
        """The open draft of this message; otherwise the refusal is sent and None returned."""
        draft = await get_draft(_db(self.ctx), inter.guild_id)
        refusal = _refusal(draft, inter.message.id)
        if refusal is not None:
            await _private(inter, refusal)
            return None
        return draft

    async def _show(self, inter: Any, draft: SetupDraft) -> None:
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

    async def _navigate(self, inter: Any, delta: int) -> None:
        await inter.response.defer()
        async with guild_lock(inter.guild_id):
            await self._navigate_locked(inter, delta)

    async def _navigate_locked(self, inter: Any, delta: int) -> None:
        draft = await self._draft(inter)
        if draft is None:
            return
        if delta > 0:
            view = await render_step(self.ctx, inter.guild, draft)
            if view.next_disabled:
                await self._show(inter, draft)  # refresh the checks, stay on this step
                return
        draft.step = max(0, min(draft.step + delta, len(STEPS) - 1))
        await self._show(inter, draft)

    async def _choose(self, inter: Any, key: str) -> None:
        await inter.response.defer()
        chosen = inter.values
        if not chosen:
            return
        async with guild_lock(inter.guild_id):
            await self._choose_locked(inter, key, chosen)

    async def _choose_locked(self, inter: Any, key: str, chosen: list[str]) -> None:
        draft = await self._draft(inter)
        if draft is None:
            return
        draft.data[key] = int(chosen[0])
        await self._show(inter, draft)

    async def _choose_channel(self, inter: Any, key: str) -> None:
        await self._choose(inter, key)

    async def _choose_role(self, inter: Any, key: str) -> None:
        await self._choose(inter, key)

    # --- control handlers ---

    async def on_back(self, inter: Any) -> None:
        await self._navigate(inter, -1)

    async def on_next(self, inter: Any) -> None:
        await self._navigate(inter, 1)

    async def on_create_role(self, inter: Any) -> None:
        await inter.response.defer()
        async with guild_lock(inter.guild_id):
            await self._create_role_locked(inter)

    async def _create_role_locked(self, inter: Any) -> None:
        draft = await self._draft(inter)
        if draft is None:
            return
        guild = inter.guild
        role = next((r for r in guild.roles if r.name == texts.INVITES_ROLE_NAME), None)
        if role is None:
            if not guild.me.guild_permissions.manage_roles:
                await _private(inter, texts.SETUP_CREATE_NEEDS_PERMISSION)
                return
            try:
                role = await guild.create_role(
                    name=texts.INVITES_ROLE_NAME, reason="RaveBot setup"
                )
            except disnake.HTTPException:
                logger.warning("Setup wizard: creating the invites role failed", exc_info=True)
                await _private(inter, texts.SETUP_CREATE_FAILED)
                return
        draft.data["invites_role_id"] = role.id
        await self._show(inter, draft)

    async def on_events_change(self, inter: Any) -> None:
        draft = await self._draft(inter)
        if draft is None:
            return
        v = values(draft.data)
        await inter.response.send_modal(
            ValuesModal(
                self,
                inter.message.id,
                "Event defaults",
                f"setup_events:{inter.message.id}",
                [
                    ("slots", "Slots per event", str(v["slots_per_event"])),
                    (
                        "start",
                        f"Start time ({zone_abbr(v['timezone'])}, HH:MM)",
                        v["default_start_time"],
                    ),
                    ("length", "Slot length (minutes)", str(v["slot_duration_minutes"])),
                    ("interval", "One event every (days)", str(v["event_interval_days"])),
                    ("ahead", "Future events kept ready", str(v["event_lookahead_count"])),
                ],
                parse_events_fields,
            )
        )

    async def on_zone(self, inter: Any) -> None:
        draft = await self._draft(inter)
        if draft is None:
            return
        await inter.response.send_modal(
            ValuesModal(
                self,
                inter.message.id,
                "Time zone",
                f"setup_zone:{inter.message.id}",
                [("zone", "Time zone (e.g. Europe/Berlin)", values(draft.data)["timezone"])],
                parse_zone_field,
            )
        )

    async def on_invites_change(self, inter: Any) -> None:
        draft = await self._draft(inter)
        if draft is None:
            return
        v = values(draft.data)
        await inter.response.send_modal(
            ValuesModal(
                self,
                inter.message.id,
                "Automatic invites",
                f"setup_invites:{inter.message.id}",
                [
                    ("days", "Days before an event", str(v["outreach_days_before"])),
                    ("hours", "Hours between invites", str(v["outreach_cooldown_hours"])),
                ],
                parse_invites_fields,
            )
        )

    async def on_toggle(self, inter: Any) -> None:
        await inter.response.defer()
        async with guild_lock(inter.guild_id):
            draft = await self._draft(inter)
            if draft is None:
                return
            draft.data["outreach_enabled"] = not values(draft.data)["outreach_enabled"]
            await self._show(inter, draft)

    async def on_activate(self, inter: Any) -> None:
        await inter.response.defer()
        await activate_from_draft(self.ctx, inter)

    async def on_save(self, inter: Any) -> None:
        await inter.response.defer()
        await save_from_draft(self.ctx, inter)

    async def on_change(self, inter: Any) -> None:
        await inter.response.defer()
        async with guild_lock(inter.guild_id):
            existing = await get_draft(_db(self.ctx), inter.guild_id)
            if existing is not None and existing.message_id != inter.message.id:
                await _private(
                    inter, texts.SETUP_ALREADY_OPEN.format(url=_jump_url(inter, existing))
                )
                return
            draft = existing or SetupDraft(
                inter.guild_id,
                inter.message.id,
                inter.channel_id,
                0,
                await prefilled_data(_db(self.ctx), inter.guild_id, inter.guild),
            )
            await self._show(inter, draft)


class ValuesModal(disnake.ui.Modal):
    """A popup with short text fields; valid input goes into the draft, nothing else."""

    def __init__(
        self,
        view: SetupWizardView,
        message_id: int,
        title: str,
        custom_id: str,
        fields: list[tuple[str, str, str]],
        parser: Callable[[dict[str, str]], tuple[dict[str, Any], list[str]]],
    ) -> None:
        self._wizard = view
        self._message_id = message_id
        self._parser = parser
        super().__init__(
            title=title,
            custom_id=custom_id,
            components=[
                disnake.ui.TextInput(
                    label=label, custom_id=fid, value=current, max_length=40, required=True
                )
                for fid, label, current in fields
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        ctx = self._wizard.ctx
        if inter.author.id not in ctx.config.admin_user_ids:
            await _private(inter, texts.SETUP_ONLY_ORGANIZERS)
            return
        parsed, errors = self._parser(dict(inter.text_values))
        if errors:
            await _private(inter, "\n".join(errors))
            return
        # Update the wizard message (the modal came from it) instead of posting a new one
        await inter.response.defer(with_message=False)
        guild_id: Any = inter.guild_id
        async with guild_lock(guild_id):
            draft = await get_draft(_db(ctx), guild_id)
            refusal = _refusal(draft, self._message_id)
            if refusal is not None or draft is None:
                await _private(inter, refusal or texts.SETUP_ALREADY_DONE)
                return
            draft.data.update(parsed)
            await self._wizard._show(inter, draft)


def registered_views(ctx: "BotContext") -> list[SetupWizardView]:
    """One view per step plus the entry view, for `bot.add_view` (persistence)."""
    views = [entry_view(ctx)]
    for step in range(len(STEPS)):
        # Control ids per step; the content is irrelevant for routing.
        probe = SetupDraft(0, 0, 0, step, {"rerun": step == len(STEPS) - 1})
        views.append(SetupWizardView(ctx, _layout(probe)))
    views.append(SetupWizardView(ctx, _layout(SetupDraft(0, 0, 0, len(STEPS) - 1, {}))))
    return views


def _layout(draft: SetupDraft) -> StepView:
    """Control ids of a step without any checks (no Discord or database access)."""
    key = step_key(draft.step)
    layouts: dict[str, list[str]] = {
        "welcome": [ID_NEXT],
        "dj_channel": [ID_DJ_CHANNEL, ID_BACK, ID_NEXT],
        "log_channel": [ID_LOG_CHANNEL, ID_BACK, ID_NEXT],
        "roles": [ID_DJ_ROLE, ID_INVITES_ROLE, ID_CREATE_ROLE, ID_BACK, ID_NEXT],
        "events": [ID_EVENTS_CHANGE, ID_ZONE, ID_BACK, ID_NEXT],
        "invites": [ID_INVITES_TOGGLE, ID_INVITES_CHANGE, ID_BACK, ID_NEXT],
        "summary": [ID_BACK, ID_SAVE if draft.data.get("rerun") else ID_ACTIVATE],
    }
    return StepView("", layouts[key])


def entry_view(ctx: "BotContext") -> SetupWizardView:
    """The view of the "already active" message: only the Change settings button."""
    return SetupWizardView(ctx, StepView("", [ID_CHANGE]))


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


def _stored_int(raw: str | None, default: int, maximum: int | None = None) -> int:
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    if value < 1 or (maximum is not None and value > maximum):
        return default
    return value


async def prefilled_data(
    db: Any, guild_id: int, guild: Any = None, rerun: bool = True
) -> dict[str, Any]:
    """Draft data from every stored value (default where missing or invalid)."""
    data: dict[str, Any] = {"rerun": rerun}
    for key in (*CHANNEL_KEYS, *ROLE_KEYS):
        raw = await db_config.get_value(db, guild_id, key)
        if raw is not None:
            try:
                data[key] = int(raw)
            except ValueError:
                pass
    if guild is not None:
        # Servers set up before the wizard use the old role names
        from bot.services import roles

        if "dj_role_id" not in data:
            role = await roles.get_dj_role(db, guild)
            if role is not None:
                data["dj_role_id"] = role.id
        if "invites_role_id" not in data:
            role = await roles.get_invites_role(db, guild)
            if role is not None:
                data["invites_role_id"] = role.id
    for key in _INT_VALUE_KEYS:
        maximum = MAX_SLOTS if key == "slots_per_event" else None
        data[key] = _stored_int(
            await db_config.get_value(db, guild_id, key), DEFAULT_VALUES[key], maximum
        )
    start = parse_hhmm(await db_config.get_value(db, guild_id, "default_start_time") or "")
    data["default_start_time"] = start or DEFAULT_VALUES["default_start_time"]
    zone = await db_config.get_value(db, guild_id, "timezone")
    data["timezone"] = zone if zone and is_valid_timezone(zone) else DEFAULT_TIMEZONE
    data["orig_timezone"] = data["timezone"]
    data["orig_invites_role_id"] = data.get("invites_role_id")
    data["outreach_enabled"] = parse_outreach_enabled(
        await db_config.get_value(db, guild_id, "outreach_enabled")
    )
    return data


def _complete(data: dict[str, Any]) -> bool:
    return all(data.get(key) is not None for key in (*CHANNEL_KEYS, *ROLE_KEYS))


async def _write_all(db: Any, guild_id: int, data: dict[str, Any]) -> None:
    """Write every setting of the draft and apply the time zone."""
    v = values(data)
    for key in (*CHANNEL_KEYS, *ROLE_KEYS):
        await db_config.set_value(db, guild_id, key, str(data[key]))
    for key, value in v.items():
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        await db_config.set_value(db, guild_id, key, text)
    set_timezone(v["timezone"])


async def activate_from_draft(ctx: "BotContext", inter: Any) -> None:
    """First activation: write the keys, set the flag, run the effects exactly once."""
    guild_id = inter.guild_id
    db = _db(ctx)
    async with guild_lock(guild_id):
        draft = await get_draft(db, guild_id)
        refusal = _refusal(draft, inter.message.id)
        if refusal is None and draft is not None and draft.data.get("rerun"):
            refusal = texts.SETUP_ALREADY_DONE
        if refusal is not None or draft is None:
            await _private(inter, refusal or texts.SETUP_ALREADY_DONE)
            return
        if not _complete(draft.data):
            await _private(inter, texts.SETUP_MISSING_VALUES)
            return
        try:
            # An explicit "false" first: a failure below must not leave a half-written
            # setup that the legacy rule (both channels stored) would count as active.
            await db_config.set_value(db, guild_id, activation.ACTIVE_KEY, "false")
            await _write_all(db, guild_id, draft.data)
            await activation.set_active(db, guild_id)
            created, posted = await _run_effects(ctx, guild_id)
            await delete_draft(db, guild_id)
        except Exception:
            logger.exception("Setup wizard: activation failed")
            await _private(inter, texts.SETUP_ERROR)
            return
        await inter.edit_original_message(
            content=texts.setup_active_message(
                created, posted, f"<#{draft.data['dj_channel']}>"
            ),
            view=None,
        )


async def _run_effects(ctx: "BotContext", guild_id: int) -> tuple[int, bool]:
    """Create the missing future events and post the first Lineup Update.

    The event list is loaded by replenish_events inside its lock, so a poll running at
    the same time cannot leave this call with a stale list.
    Returns (events created, whether the Lineup Update was posted).
    """
    created = 0
    posted = False
    if ctx.notion_client is not None:
        created = await replenish_events(_db(ctx), ctx.notion_client, guild_id)
        if ctx.lineup_service is not None:
            events = await ctx.notion_client.list_events()
            posted = bool(await ctx.lineup_service.refresh(events))
    return created, posted


async def save_from_draft(ctx: "BotContext", inter: Any) -> None:
    """Re-run: store the changed values; no events are created and nothing is reposted."""
    guild_id = inter.guild_id
    async with guild_lock(guild_id):
        draft = await get_draft(_db(ctx), guild_id)
        refusal = _refusal(draft, inter.message.id)
        if refusal is None and draft is not None and not draft.data.get("rerun"):
            refusal = texts.SETUP_NOT_A_RERUN
        if refusal is not None or draft is None:
            await _private(inter, refusal or texts.SETUP_ALREADY_DONE)
            return
        if not _complete(draft.data):
            await _private(inter, texts.SETUP_MISSING_VALUES)
            return
        try:
            await _write_all(_db(ctx), guild_id, draft.data)
            await delete_draft(_db(ctx), guild_id)
        except Exception:
            logger.exception("Setup wizard: saving failed")
            await _private(inter, texts.SETUP_ERROR)
            return
        await inter.edit_original_message(content=texts.SETUP_SAVED, view=None)
