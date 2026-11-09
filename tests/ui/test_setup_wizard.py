"""Tests for the setup wizard view (bot/ui/setup_wizard.py)."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db.setup_drafts import SetupDraft
from bot.services import activation as _activation
from bot.ui import setup_wizard as sw
from bot.ui import texts

REAL_IS_ACTIVE = _activation.is_active  # captured before the autouse conftest patch

GUILD = 10
ADMIN = 1
FULL = {"dj_channel": 100, "log_channel": 200, "dj_role_id": 300, "invites_role_id": 400}
SUMMARY = len(sw.STEPS) - 1
INVITES = sw.STEPS.index("invites")


class Store:
    """In-memory drafts and config standing in for the database."""

    def __init__(self) -> None:
        self.drafts: dict[int, SetupDraft] = {}
        self.config: dict[tuple[int, str], str] = {}
        self.writes: list[tuple[str, str]] = []

    async def get_draft(self, db, guild_id):
        d = self.drafts.get(guild_id)
        if d is None:
            return None
        return SetupDraft(d.guild_id, d.message_id, d.channel_id, d.step, dict(d.data))

    async def save_draft(self, db, guild_id, message_id, channel_id, step, data):
        self.drafts[guild_id] = SetupDraft(guild_id, message_id, channel_id, step, dict(data))

    async def delete_draft(self, db, guild_id):
        self.drafts.pop(guild_id, None)

    async def get_value(self, db, guild_id, key):
        return self.config.get((guild_id, key))

    async def is_active(self, db, guild_id):
        flag = self.config.get((guild_id, "bot_active"))
        if flag is not None:
            return flag == "true"
        return (guild_id, "dj_channel") in self.config and (guild_id, "log_channel") in self.config

    async def set_value(self, db, guild_id, key, value):
        self.config[(guild_id, key)] = value
        self.writes.append((key, value))


@pytest.fixture(autouse=True)
def _reset_zone():
    from bot import timezone

    before = timezone.get_timezone().key
    yield
    timezone.set_timezone(before)


@pytest.fixture
def store():
    s = Store()
    patches = [
        patch("bot.ui.setup_wizard.get_draft", s.get_draft),
        patch("bot.ui.setup_wizard.save_draft", s.save_draft),
        patch("bot.ui.setup_wizard.delete_draft", s.delete_draft),
        patch("bot.cogs.setup.get_draft", s.get_draft),
        patch("bot.cogs.setup.save_draft", s.save_draft),
        patch("bot.ui.setup_wizard.db_config.get_value", s.get_value),
        patch("bot.ui.setup_wizard.db_config.set_value", s.set_value),
        patch("bot.services.activation.get_value", s.get_value),
        patch("bot.services.activation.set_value", s.set_value),
        patch("bot.services.activation.is_active", s.is_active),
    ]
    for p in patches:
        p.start()
    sw._guild_locks.clear()
    yield s
    for p in patches:
        p.stop()


def make_ctx(events=None):
    ctx = MagicMock()
    ctx.config.admin_user_ids = frozenset({ADMIN})
    ctx.is_admin = lambda user_id: user_id == ADMIN
    ctx.db = MagicMock()
    ctx.db.fetchval = AsyncMock(return_value=1)
    ctx.notion_client.list_events = AsyncMock(return_value=events or [])
    ctx.lineup_service.refresh = AsyncMock(return_value=True)
    return ctx


def make_guild(
    perms=None,
    channel_ids=(100, 200),
    role_ids=(300, 400, 555),
    role_positions=None,
    manage_roles=True,
):
    role_positions = role_positions or {}
    perms = perms or {"send_messages": True, "embed_links": True}
    guild = MagicMock()
    guild.id = GUILD
    guild.me.guild_permissions = SimpleNamespace(manage_roles=manage_roles)
    guild.me.top_role = SimpleNamespace(position=10)

    def get_role(rid):
        if rid not in role_ids:
            return None
        return SimpleNamespace(id=rid, name=f"role{rid}", position=role_positions.get(rid, 1))

    guild.get_role = get_role
    guild.create_role = AsyncMock(return_value=SimpleNamespace(id=555, name="Active Booking"))

    def get_channel(cid):
        if cid not in channel_ids:
            return None
        ch = MagicMock()
        ch.id = cid
        ch.name = f"chan{cid}"
        ch.permissions_for = MagicMock(return_value=SimpleNamespace(**perms))
        return ch

    guild.get_channel = get_channel
    return guild


def make_inter(guild, user=ADMIN, message_id=55, values=None):
    inter = MagicMock()
    inter.author.id = user
    inter.guild_id = GUILD
    inter.guild = guild
    inter.channel_id = 7
    inter.message.id = message_id
    inter.values = values or []
    state = {"done": False}

    async def _defer(*a, **k):
        state["done"] = True

    inter.response.defer = AsyncMock(side_effect=_defer)
    inter.response.send_message = AsyncMock()
    inter.response.is_done = MagicMock(side_effect=lambda: state["done"])
    inter.followup.send = AsyncMock()
    inter.edit_original_message = AsyncMock()
    return inter


STEP = {key: index for index, key in enumerate(sw.STEPS)}


def seed(store, step=0, data=None, message_id=55):
    store.drafts[GUILD] = SetupDraft(GUILD, message_id, 7, step, data or {})


def last_content(inter):
    return inter.edit_original_message.await_args.kwargs["content"]


def last_view(inter):
    return inter.edit_original_message.await_args.kwargs["view"]


def ids(view):
    return {c.custom_id for c in view.children}


def disabled_next(view):
    return next(c for c in view.children if c.custom_id == sw.ID_NEXT).disabled


# ---------- access ----------

CONTROLS = ["back", "next", "dj_channel", "log_channel", "activate", "save", "change"]


@pytest.mark.asyncio
async def test_every_control_refuses_non_admin(store):
    ctx = make_ctx()
    view = sw.SetupWizardView(ctx)
    registered = {c.custom_id for v in sw.registered_views(ctx) for c in v.children}
    assert registered == {
        sw.ID_BACK, sw.ID_NEXT, sw.ID_DJ_CHANNEL, sw.ID_LOG_CHANNEL, sw.ID_DJ_ROLE,
        sw.ID_INVITES_ROLE, sw.ID_CREATE_ROLE, sw.ID_VALUE, sw.ID_OTHER,
        sw.ID_SCHEDULE_TOGGLE,
        sw.ID_INVITES_TOGGLE, sw.ID_INVITES_CHANGE, sw.ID_ACTIVATE, sw.ID_SAVE, sw.ID_CHANGE,
    }
    assert all(v.timeout is None for v in sw.registered_views(ctx))
    for _ in sorted(registered):
        inter = make_inter(make_guild(), user=999)
        assert await view.interaction_check(inter) is False
        inter.response.send_message.assert_awaited_once_with(
            "Only organizers can change the setup.", ephemeral=True
        )
    assert await view.interaction_check(make_inter(make_guild())) is True
    assert view.timeout is None
    from bot.ui.gates import GatedView

    assert not issubclass(sw.SetupWizardView, GatedView)


# ---------- steps ----------

@pytest.mark.asyncio
async def test_welcome_shows_checks_and_start(store):
    ctx = make_ctx()
    seed(store, 0)
    draft = store.drafts[GUILD]
    view = await sw.render_step(ctx, make_guild(), draft)
    assert "Discord connection" in view.content and "Database" in view.content
    assert "Notion: Events, Slots and DJs found" in view.content
    assert view.next_label == "Start" and not view.next_disabled
    assert sw.ID_BACK not in view.visible


@pytest.mark.asyncio
async def test_welcome_failed_check_disables_start(store):
    ctx = make_ctx()
    ctx.notion_client.list_events = AsyncMock(side_effect=RuntimeError("x"))
    view = await sw.render_step(ctx, make_guild(), SetupDraft(GUILD, 55, 7, 0, {}))
    assert view.next_disabled
    assert "✗ Notion" in view.content


@pytest.mark.asyncio
async def test_choose_channel_stores_only_in_draft(store):
    ctx = make_ctx()
    seed(store, 1)
    inter = make_inter(make_guild(), values=["100"])
    await sw.SetupWizardView(ctx)._choose_channel(inter, "dj_channel")
    assert store.drafts[GUILD].data["dj_channel"] == 100
    assert store.writes == []
    assert "I can send messages and embeds in #chan100" in last_content(inter)
    assert not disabled_next(last_view(inter))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("perms", "named"),
    [
        ({"send_messages": False, "embed_links": True}, "Send Messages"),
        ({"send_messages": True, "embed_links": False}, "Embed Links"),
    ],
)
async def test_permission_check_blocks_next_and_names_permission(store, perms, named):
    ctx = make_ctx()
    seed(store, 2, FULL)
    guild = make_guild(perms)
    inter = make_inter(guild, values=["200"])
    view = sw.SetupWizardView(ctx)
    await view._choose_channel(inter, "log_channel")
    assert named in last_content(inter)
    assert disabled_next(last_view(inter))
    # a forged Next press does not advance either
    nav = make_inter(guild)
    await view._navigate(nav, 1)
    assert store.drafts[GUILD].step == 2
    assert named in last_content(nav)


@pytest.mark.asyncio
async def test_navigation_back_next_and_summary(store):
    ctx = make_ctx()
    guild = make_guild()
    seed(store, 0)
    view = sw.SetupWizardView(ctx)
    inter = make_inter(guild)
    await view._navigate(inter, 1)
    assert store.drafts[GUILD].step == 1
    assert disabled_next(last_view(inter))  # no channel yet
    assert f"Step 2 of {sw.total_steps()}" in last_content(inter)
    store.drafts[GUILD].data.update(FULL)
    for expected in range(2, SUMMARY + 1):
        store.drafts[GUILD].step = expected - 1
        inter = make_inter(guild)
        await view._navigate(inter, 1)
        assert store.drafts[GUILD].step == expected
    assert ids(last_view(inter)) == {sw.ID_BACK, sw.ID_ACTIVATE}
    assert "<#100>" in last_content(inter) and "<#200>" in last_content(inter)
    inter = make_inter(guild)
    await view._navigate(inter, -1)
    assert store.drafts[GUILD].step == SUMMARY - 1
    assert store.writes == []


def test_total_steps_follows_step_list():
    assert sw.total_steps() == len(sw.STEPS) - 1 == 13
    assert sw.STEPS[-1] == "summary"
    assert sw.STEPS == [
        "welcome", "dj_channel", "log_channel", "roles", "slots", "start_time", "slot_length",
        "schedule", "interval", "first_date", "lookahead", "timezone", "invites", "summary",
    ]


# ---------- activation ----------

def _event(date):
    return SimpleNamespace(date=date)


@pytest.mark.asyncio
async def test_activate_writes_keys_flag_and_runs_effects_once_on_double_press(store):
    ctx = make_ctx(events=[_event("e")])
    seed(store, SUMMARY, FULL)
    store.config[(GUILD, "slots_per_event")] = "4"
    i1, i2 = make_inter(make_guild()), make_inter(make_guild())
    async def slow(*a, **k):
        await asyncio.sleep(0)
        return 6

    ctx.lineup_service.refresh = AsyncMock(side_effect=slow)
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(side_effect=slow)) as rep:
        await i1.response.defer()
        await i2.response.defer()
        await asyncio.gather(sw.activate_from_draft(ctx, i1), sw.activate_from_draft(ctx, i2))
    assert rep.await_count == 1
    assert rep.await_args.args[2] == GUILD
    assert ctx.lineup_service.refresh.await_count == 1
    c = {k: v for (g, k), v in store.config.items()}
    assert c["dj_channel"] == "100" and c["log_channel"] == "200"
    assert c["dj_role_id"] == "300" and c["invites_role_id"] == "400"
    assert c["bot_active"] == "true"
    assert c["slots_per_event"] == "6"  # the draft value wins over an older stored value
    assert c["timezone"] == "Europe/Berlin"
    assert c["outreach_enabled"] == "true" and c["outreach_days_before"] == "14"
    assert c["outreach_cooldown_hours"] == "12"
    assert c["default_start_time"] == "20:00" and c["slot_duration_minutes"] == "60"
    assert c["event_interval_days"] == "7" and c["event_lookahead_count"] == "6"
    assert GUILD not in store.drafts
    done = [i for i in (i1, i2) if i.edit_original_message.await_count]
    assert len(done) == 1
    assert "RaveBot is active" in last_content(done[0])
    refused = [i for i in (i1, i2) if not i.edit_original_message.await_count][0]
    refused.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_button_callback_defers_and_activates(store):
    ctx = make_ctx()
    seed(store, SUMMARY, FULL)
    inter = make_inter(make_guild())
    view = sw.SetupWizardView(ctx)
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=0)):
        await view.on_activate(inter)
    inter.response.defer.assert_awaited_once()
    assert store.config[(GUILD, "bot_active")] == "true"


@pytest.mark.asyncio
async def test_activate_with_missing_channel_writes_nothing(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {"dj_channel": 100})
    inter = make_inter(make_guild())
    await inter.response.defer()
    await sw.activate_from_draft(ctx, inter)
    assert store.writes == []
    assert GUILD in store.drafts
    inter.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_failure_keeps_draft_for_retry(store):
    ctx = make_ctx()
    seed(store, SUMMARY, FULL)
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(side_effect=RuntimeError("x"))):
        await sw.activate_from_draft(ctx, inter)
    assert GUILD in store.drafts
    inter.followup.send.assert_awaited_once_with(texts.SETUP_ERROR, ephemeral=True)


@pytest.mark.asyncio
async def test_activate_on_foreign_message_is_refused(store):
    ctx = make_ctx()
    seed(store, SUMMARY, FULL, message_id=55)
    inter = make_inter(make_guild(), message_id=99)
    await sw.activate_from_draft(ctx, inter)
    assert store.writes == []


@pytest.mark.asyncio
async def test_restart_rebuilds_view_from_stored_draft(store):
    guild = make_guild()
    seed(store, 1, {})
    ctx1 = make_ctx()
    inter = make_inter(guild, values=["100"])
    await sw.SetupWizardView(ctx1)._choose_channel(inter, "dj_channel")
    # restart: a new view and ctx know nothing but the stored draft
    ctx2 = make_ctx()
    view2 = sw.SetupWizardView(ctx2)
    await view2._navigate(make_inter(guild), 1)
    assert store.drafts[GUILD].step == 2
    await view2._choose_channel(make_inter(guild, values=["200"]), "log_channel")
    await view2._navigate(make_inter(guild), 1)
    await view2._choose_role(make_inter(guild, values=["300"]), "dj_role_id")
    await view2._choose_role(make_inter(guild, values=["400"]), "invites_role_id")
    await view2._navigate(make_inter(guild), 1)
    assert store.drafts[GUILD].step == 4
    # restart again in the middle of the events step
    view3 = sw.SetupWizardView(make_ctx())
    await view3._navigate(make_inter(guild), 1)
    await view3._navigate(make_inter(guild), 1)
    assert store.drafts[GUILD].step == 6
    inter = make_inter(guild)
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=0)):
        await sw.activate_from_draft(ctx2, inter)
    assert store.config[(GUILD, "dj_channel")] == "100"
    assert store.config[(GUILD, "log_channel")] == "200"
    assert store.config[(GUILD, "dj_role_id")] == "300"


# ---------- change settings and save ----------

@pytest.mark.asyncio
async def test_change_settings_prefills_all_stored_values(store):
    ctx = make_ctx()
    store.config.update({
        (GUILD, "dj_channel"): "100", (GUILD, "log_channel"): "200",
        (GUILD, "dj_role_id"): "300", (GUILD, "invites_role_id"): "400",
        (GUILD, "slots_per_event"): "4", (GUILD, "default_start_time"): "18:30",
        (GUILD, "slot_duration_minutes"): "90", (GUILD, "event_interval_days"): "14",
        (GUILD, "event_lookahead_count"): "3", (GUILD, "timezone"): "Europe/London",
        (GUILD, "outreach_enabled"): "false", (GUILD, "outreach_days_before"): "10",
        (GUILD, "outreach_cooldown_hours"): "24", (GUILD, "bot_active"): "true",
    })
    inter = make_inter(make_guild())
    await sw.SetupWizardView(ctx).on_change(inter)
    draft = store.drafts[GUILD]
    assert draft.data == {
        "rerun": True, **FULL, "slots_per_event": 4, "default_start_time": "18:30",
        "slot_duration_minutes": 90, "event_interval_days": 14, "event_lookahead_count": 3,
        "timezone": "Europe/London", "outreach_enabled": False,
        "outreach_days_before": 10, "outreach_cooldown_hours": 24,
        "orig_timezone": "Europe/London", "orig_invites_role_id": 400,
        "event_schedule_enabled": True, "first_event_date": "",
    }
    assert draft.message_id == 55 and draft.step == 0
    assert store.writes == []


@pytest.mark.asyncio
async def test_save_writes_all_keys_applies_zone_and_runs_no_effects(store):
    from bot import timezone

    ctx = make_ctx()
    guild = make_guild()
    seed(store, SUMMARY, {"rerun": True, **FULL, "timezone": "Europe/London", "slots_per_event": 3,
                    "outreach_enabled": False})
    inter = make_inter(guild)
    await inter.response.defer()
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock()) as rep:
        await sw.save_from_draft(ctx, inter)
    rep.assert_not_awaited()
    ctx.lineup_service.refresh.assert_not_awaited()
    c = {k: v for (g, k), v in store.config.items()}
    assert set(c) == {
        "dj_channel", "log_channel", "dj_role_id", "invites_role_id", "slots_per_event",
        "default_start_time", "slot_duration_minutes", "event_interval_days",
        "event_lookahead_count", "timezone", "outreach_enabled", "outreach_days_before",
        "outreach_cooldown_hours", "event_schedule_enabled", "first_event_date",
    }
    assert c["slots_per_event"] == "3" and c["outreach_enabled"] == "false"
    assert timezone.get_timezone().key == "Europe/London"
    assert "bot_active" not in c
    assert GUILD not in store.drafts
    assert last_content(inter).startswith("Settings saved.")


@pytest.mark.asyncio
async def test_activate_applies_zone_and_writes_roles(store):
    from bot import timezone

    ctx = make_ctx()
    seed(store, SUMMARY, {**FULL, "timezone": "Europe/London"})
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=0)):
        await sw.activate_from_draft(ctx, inter)
    assert store.config[(GUILD, "timezone")] == "Europe/London"
    assert timezone.get_timezone().key == "Europe/London"
    assert store.config[(GUILD, "dj_role_id")] == "300"


@pytest.mark.asyncio
async def test_activate_requires_roles(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {"dj_channel": 100, "log_channel": 200})
    inter = make_inter(make_guild())
    await inter.response.defer()
    await sw.activate_from_draft(ctx, inter)
    assert store.writes == []


# ---------- roles ----------

@pytest.mark.asyncio
async def test_role_selects_store_in_draft_only_and_check_blocks_next(store):
    ctx = make_ctx()
    seed(store, 3, {})
    view = sw.SetupWizardView(ctx)
    guild = make_guild()
    inter = make_inter(guild, values=["300"])
    await view._choose_role(inter, "dj_role_id")
    assert disabled_next(last_view(inter))  # the invites role is still missing
    inter = make_inter(guild, values=["400"])
    await view._choose_role(inter, "invites_role_id")
    assert not disabled_next(last_view(inter))
    assert "I can manage these roles" in last_content(inter)
    assert store.drafts[GUILD].data == {"dj_role_id": 300, "invites_role_id": 400}
    assert store.writes == []
    assert f"Step 4 of {sw.total_steps()}" in last_content(inter)


@pytest.mark.asyncio
async def test_role_above_bot_blocks_next_and_names_role(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_role_id": 300, "invites_role_id": 400})
    guild = make_guild(role_positions={400: 10})
    view = await sw.render_step(ctx, guild, store.drafts[GUILD])
    assert view.next_disabled and "role400" in view.content


@pytest.mark.asyncio
async def test_roles_without_manage_roles_block_next(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_role_id": 300, "invites_role_id": 400})
    view = await sw.render_step(ctx, make_guild(manage_roles=False), store.drafts[GUILD])
    assert view.next_disabled and "Manage Roles" in view.content


@pytest.mark.asyncio
async def test_create_role_creates_and_selects_it(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_role_id": 300})
    guild = make_guild()
    inter = make_inter(guild)
    await sw.SetupWizardView(ctx).on_create_role(inter)
    guild.create_role.assert_awaited_once()
    assert guild.create_role.await_args.kwargs["name"] == "Active Booking"
    assert store.drafts[GUILD].data["invites_role_id"] == 555
    assert not disabled_next(last_view(inter))
    assert store.writes == []


@pytest.mark.asyncio
async def test_create_role_without_permission_reports_privately_and_stays(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_role_id": 300})
    guild = make_guild(manage_roles=False)
    inter = make_inter(guild)
    await sw.SetupWizardView(ctx).on_create_role(inter)
    guild.create_role.assert_not_awaited()
    inter.followup.send.assert_awaited_once_with(
        texts.SETUP_CREATE_NEEDS_PERMISSION, ephemeral=True
    )
    assert "invites_role_id" not in store.drafts[GUILD].data
    assert store.drafts[GUILD].step == 3
    assert "Manage Roles" in texts.SETUP_CREATE_NEEDS_PERMISSION


@pytest.mark.asyncio
async def test_selects_show_stored_choice_as_default(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_role_id": 300, "invites_role_id": 400})
    step = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    view = sw.SetupWizardView(ctx, step)
    selects = [c for c in view.children if getattr(c, "default_values", None)]
    assert sorted(d.id for c in selects for d in c.default_values) == [300, 400]


# ---------- events, zone, invites popups ----------

async def _open_modal(store, ctx, step, handler_name, data=None):
    seed(store, step, data or {})
    inter = make_inter(make_guild())
    inter.response.send_modal = AsyncMock()
    await getattr(sw.SetupWizardView(ctx), handler_name)(inter)
    return inter.response.send_modal.await_args.args[0]


def _modal_inter(fields):
    inter = make_inter(make_guild())
    inter.text_values = fields
    return inter


VALUE_STEPS = [
    ("slots", "slots_per_event", "7", 7),
    ("start_time", "default_start_time", "22:00", "22:00"),
    ("slot_length", "slot_duration_minutes", "90", 90),
    ("interval", "event_interval_days", "21", 21),
    ("first_date", "event_first_date_placeholder", "", ""),
    ("lookahead", "event_lookahead_count", "8", 8),
    ("timezone", "timezone", "Europe/London", "Europe/London"),
]
VALUE_STEPS[4] = ("first_date", "first_event_date", "2099-01-01", "2099-01-01")


def _select(view):
    return next(c for c in view.children if c.custom_id == sw.ID_VALUE)


@pytest.mark.asyncio
async def test_slots_step_renders_dropdown_with_current_selected(store):
    ctx = make_ctx()
    seed(store, STEP["slots"], {"slots_per_event": 4})
    step = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    assert step.visible == [sw.ID_VALUE, sw.ID_OTHER, sw.ID_BACK, sw.ID_NEXT]
    assert f"Step {STEP['slots'] + 1} of {sw.total_steps()}" in step.content
    assert "Slots per event" in step.content and "4" in step.content
    select = _select(sw.SetupWizardView(ctx, step))
    chosen = [o.value for o in select.options if o.default]
    assert chosen == ["4"] and len(select.options) <= 25


@pytest.mark.asyncio
@pytest.mark.parametrize(("step_key", "key", "raw", "expected"), VALUE_STEPS)
async def test_value_select_stores_in_draft_only_and_rerenders(
    store, step_key, key, raw, expected
):
    ctx = make_ctx()
    seed(store, STEP[step_key], {})
    inter = make_inter(make_guild(), values=[raw])
    await sw.SetupWizardView(ctx).on_value_select(inter)
    assert store.drafts[GUILD].data[key] == expected
    assert store.drafts[GUILD].step == STEP[step_key]
    assert store.writes == []
    assert ids(last_view(inter)) == {sw.ID_VALUE, sw.ID_OTHER, sw.ID_BACK, sw.ID_NEXT}


@pytest.mark.asyncio
async def test_value_select_with_no_values_is_a_no_op(store):
    ctx = make_ctx()
    seed(store, STEP["slots"], {})
    inter = make_inter(make_guild(), values=[])
    await sw.SetupWizardView(ctx).on_value_select(inter)
    inter.edit_original_message.assert_not_awaited()
    assert store.drafts[GUILD].data == {}


@pytest.mark.asyncio
async def test_stored_out_of_range_value_stays_selected_and_survives_next(store):
    ctx = make_ctx()
    seed(store, STEP["interval"], {"event_interval_days": 10})
    step = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    select = _select(sw.SetupWizardView(ctx, step))
    assert [o.value for o in select.options if o.default] == ["10"]
    await sw.SetupWizardView(ctx)._navigate(make_inter(make_guild()), 1)
    assert store.drafts[GUILD].data.get("event_interval_days", 10) == 10
    assert store.drafts[GUILD].step == STEP["interval"] + 1


@pytest.mark.asyncio
async def test_other_opens_one_field_popup_with_current_value(store):
    ctx = make_ctx()
    modal = await _open_modal(store, ctx, STEP["interval"], "on_other", {"event_interval_days": 10})
    assert len(modal.components) == 1
    field = modal.components[0].children[0] if hasattr(modal.components[0], "children") \
        else modal.components[0]
    assert field.value == "10"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_key", "bad", "name"),
    [
        ("slots", "0", "Slots per event"),
        ("slots", "101", "Slots per event"),
        ("start_time", "24:00", "Start time"),
        ("start_time", "noon", "Start time"),
        ("slot_length", "1441", "Slot length"),
        ("interval", "366", "One event every"),
        ("first_date", "2020-01-01", "First event date"),
        ("first_date", "14.11.2026", "First event date"),
        ("lookahead", "53", "Future events kept ready"),
        ("timezone", "Mars/Base", "Time zone"),
    ],
)
async def test_other_popup_rejects_invalid_naming_field(store, step_key, bad, name):
    ctx = make_ctx()
    modal = await _open_modal(store, ctx, STEP[step_key], "on_other")
    inter = _modal_inter({"v": bad})
    await modal.callback(inter)
    text = inter.response.send_message.await_args.args[0]
    assert name in text
    assert inter.response.send_message.await_args.kwargs["ephemeral"] is True
    assert store.drafts[GUILD].data == {}


@pytest.mark.asyncio
async def test_other_popup_trims_and_normalizes(store):
    ctx = make_ctx()
    modal = await _open_modal(store, ctx, STEP["start_time"], "on_other")
    inter = _modal_inter({"v": " 9:30 "})
    await modal.callback(inter)
    assert store.drafts[GUILD].data["default_start_time"] == "09:30"
    assert store.writes == []
    assert "09:30" in last_content(inter)


@pytest.mark.asyncio
async def test_timezone_choice_is_applied_only_on_activate_or_save(store):
    from bot import timezone

    ctx = make_ctx()
    modal = await _open_modal(store, ctx, STEP["timezone"], "on_other")
    await modal.callback(_modal_inter({"v": "Europe/London"}))
    assert store.drafts[GUILD].data["timezone"] == "Europe/London"
    assert timezone.get_timezone().key != "Europe/London"


@pytest.mark.asyncio
async def test_value_controls_refuse_non_admin(store):
    ctx = make_ctx()
    view = sw.SetupWizardView(ctx)
    for _ in (sw.ID_VALUE, sw.ID_OTHER):
        inter = make_inter(make_guild(), user=999)
        assert await view.interaction_check(inter) is False


@pytest.mark.asyncio
async def test_old_step_index_beyond_range_is_clamped(store):
    ctx = make_ctx()
    seed(store, 99, {"rerun": True, **FULL})
    step = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    assert step.visible == [sw.ID_BACK, sw.ID_SAVE]

@pytest.mark.asyncio
async def test_invites_toggle_and_popup(store):
    ctx = make_ctx()
    seed(store, INVITES, {})
    inter = make_inter(make_guild())
    await sw.SetupWizardView(ctx).on_toggle(inter)
    assert store.drafts[GUILD].data["outreach_enabled"] is False
    assert "Automatic invites: off" in last_content(inter)
    assert f"Step {INVITES + 1} of {sw.total_steps()}" in last_content(inter)
    toggle = next(c for c in last_view(inter).children if c.custom_id == sw.ID_INVITES_TOGGLE)
    assert toggle.label == "Turn on"
    modal = await _open_modal(store, ctx, INVITES, "on_invites_change")
    for fields, name in (({"days": "0", "hours": "5"}, "Days before"),
                         ({"days": "5", "hours": "x"}, "Hours between")):
        bad = _modal_inter(fields)
        await modal.callback(bad)
        assert name in bad.response.send_message.await_args.args[0]
    good = _modal_inter({"days": "3", "hours": "8"})
    await modal.callback(good)
    d = store.drafts[GUILD].data
    assert (d["outreach_days_before"], d["outreach_cooldown_hours"]) == (3, 8)
    assert store.writes == []


@pytest.mark.asyncio
async def test_modal_refuses_non_admin(store):
    ctx = make_ctx()
    modal = await _open_modal(store, ctx, STEP["timezone"], "on_other")
    inter = _modal_inter({"v": "Europe/London"})
    inter.author.id = 999
    await modal.callback(inter)
    inter.response.send_message.assert_awaited_once_with(
        texts.SETUP_ONLY_ORGANIZERS, ephemeral=True
    )
    assert store.drafts[GUILD].data == {}


async def _added_admin_ctx(added: int):
    from bot.services.admin_registry import AdminRegistry

    ctx = make_ctx()
    ctx.is_admin = lambda user_id: ctx.admins.is_admin(user_id)
    ctx.admins = AdminRegistry(frozenset({ADMIN}), ctx.db)
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)):
        await ctx.admins.add(added)
    return ctx


@pytest.mark.asyncio
async def test_interaction_check_accepts_an_added_admin(store):
    ctx = await _added_admin_ctx(777)
    view = sw.SetupWizardView(ctx)
    assert await view.interaction_check(make_inter(make_guild(), user=777)) is True
    stranger = make_inter(make_guild(), user=999)
    assert await view.interaction_check(stranger) is False


@pytest.mark.asyncio
async def test_modal_accepts_an_added_admin(store):
    ctx = await _added_admin_ctx(777)
    modal = await _open_modal(store, ctx, STEP["timezone"], "on_other")
    inter = _modal_inter({"v": "Europe/London"})
    inter.author.id = 777
    await modal.callback(inter)
    assert store.drafts[GUILD].data != {}


# ---------- summary ----------

@pytest.mark.asyncio
async def test_summary_shows_values_and_warns_on_changed_zone_and_role(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {"rerun": True, **FULL, "timezone": "Europe/London",
                    "orig_timezone": "Europe/Berlin", "orig_invites_role_id": 999})
    view = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    assert "the real times of existing events change" in view.content
    assert "members are not moved to the new role" in view.content
    abbr = sw.zone_abbr("Europe/London")
    assert f"Events: 6 slots, 20:00 {abbr}, 60 min" in view.content
    assert "Scheduled events: every 7 days, 6 ahead" in view.content
    assert "DJ role: @role300" in view.content
    assert "Automatic invites: on, 14 days before, 12 h wait" in view.content
    assert f"Step {sw.total_steps()} of {sw.total_steps()}" in view.content
    assert ids(sw.SetupWizardView(ctx, view)) == {sw.ID_BACK, sw.ID_SAVE}


@pytest.mark.asyncio
async def test_summary_has_no_warnings_when_unchanged(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {"rerun": True, **FULL, "orig_timezone": "Europe/Berlin",
                    "orig_invites_role_id": 400})
    view = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    assert "Warning" not in view.content


@pytest.mark.asyncio
async def test_activate_refuses_rerun_draft(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {"rerun": True, **FULL})
    await sw.activate_from_draft(ctx, make_inter(make_guild()))
    assert store.writes == []


# ---------- texts ----------

def test_wizard_texts_have_no_utc_or_em_dash():
    strings = [v for k, v in vars(texts).items() if k.startswith("SETUP_") and isinstance(v, str)]
    strings += [
        texts.setup_welcome(3, [(True, "a"), (False, "b")]),
        texts.setup_channel_step(2, 3, "DJ channel", texts.SETUP_DJ_CHANNEL_QUESTION, "<#1>", None),
        texts.setup_channel_ok("x"),
        texts.setup_channel_missing("x", ["Send Messages"]),
        texts.setup_summary(["a"], False),
        texts.setup_roles_step(4, 6, "@a", None, texts.SETUP_ROLES_OK),
        texts.setup_value_step(5, 13, "Slots per event", "How many?", "6", "note"),
        texts.setup_invites_step(6, 6, sw.values({})),
        texts.SETUP_ZONE_CHANGED_WARNING,
        texts.SETUP_INVITES_ROLE_WARNING,
        texts.setup_active_message(3, True, "<#1>"),
    ]
    for s in strings:
        assert "—" not in s
        assert not re.search(r"\bUTC\b", s)


# ---------- fix round 1 ----------

@pytest.mark.asyncio
async def test_empty_select_values_is_a_no_op(store):
    ctx = make_ctx()
    seed(store, 1)
    inter = make_inter(make_guild(), values=[])
    await sw.SetupWizardView(ctx)._choose_channel(inter, "dj_channel")
    assert store.drafts[GUILD].data == {}
    inter.edit_original_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_refuses_first_run_draft(store):
    ctx = make_ctx()
    seed(store, SUMMARY, FULL)
    inter = make_inter(make_guild())
    await inter.response.defer()
    await sw.save_from_draft(ctx, inter)
    assert store.writes == [] and GUILD in store.drafts
    inter.followup.send.assert_awaited_once_with(texts.SETUP_NOT_A_RERUN, ephemeral=True)


def test_active_text_only_claims_what_happened():
    both = texts.setup_active_message(6, True, "<#1>")
    assert "I created your first 6 events in Notion" in both
    assert "posted the Lineup Update in <#1>" in both
    none = texts.setup_active_message(0, False, "<#1>")
    assert "created" not in none and "Lineup Update" not in none
    only_events = texts.setup_active_message(3, False, "<#1>")
    assert "Lineup Update" not in only_events and "first 3 events" in only_events


@pytest.mark.asyncio
async def test_activate_text_reflects_refresh_result(store):
    ctx = make_ctx()
    ctx.lineup_service.refresh = AsyncMock(return_value=False)
    seed(store, SUMMARY, FULL)
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=2)):
        await sw.activate_from_draft(ctx, inter)
    assert "Lineup Update" not in last_content(inter)
    assert "first 2 events" in last_content(inter)


@pytest.mark.asyncio
async def test_rerun_welcome_does_not_claim_idle(store):
    ctx = make_ctx()
    view = await sw.render_step(ctx, make_guild(), SetupDraft(GUILD, 55, 7, 0, {"rerun": True}))
    assert "stay idle" not in view.content and "Save" in view.content
    first = await sw.render_step(ctx, make_guild(), SetupDraft(GUILD, 55, 7, 0, {}))
    assert "stay idle" in first.content


@pytest.mark.asyncio
async def test_replaced_draft_says_outdated(store):
    ctx = make_ctx()
    seed(store, 1, {}, message_id=99)  # newer wizard message
    view = sw.SetupWizardView(ctx)
    inter = make_inter(make_guild(), message_id=55)
    await view._navigate(inter, 1)
    inter.followup.send.assert_awaited_once_with(texts.SETUP_OUTDATED, ephemeral=True)
    inter = make_inter(make_guild(), message_id=55)
    await inter.response.defer()
    await sw.activate_from_draft(ctx, inter)
    inter.followup.send.assert_awaited_once_with(texts.SETUP_OUTDATED, ephemeral=True)


# ---------- fix round 1 (5b review) ----------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "fields"),
    [
        ("on_other", {"v": "5"}),
        ("on_invites_change", {"days": "3", "hours": "8"}),
    ],
)
async def test_popup_submit_defers_without_message_and_edits_wizard(store, handler, fields):
    ctx = make_ctx()
    step = STEP["slots"] if handler == "on_other" else INVITES
    modal = await _open_modal(store, ctx, step, handler)
    inter = _modal_inter(fields)
    await modal.callback(inter)
    inter.response.defer.assert_awaited_once_with(with_message=False)
    inter.edit_original_message.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()
    inter.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_server_zone_change_warns_without_stored_zone(store):
    store.config[(GUILD, "bot_active")] = "true"
    store.config[(GUILD, "dj_channel")] = "100"
    store.config[(GUILD, "log_channel")] = "200"
    ctx = make_ctx()
    guild = make_guild()
    data = await sw.prefilled_data(ctx.db, GUILD, guild)
    assert data["orig_timezone"] == "Europe/Berlin"
    seed(store, SUMMARY, {**data, **FULL, "timezone": "Europe/London"})
    view = await sw.render_step(ctx, guild, store.drafts[GUILD])
    assert "the real times of existing events change" in view.content


@pytest.mark.asyncio
async def test_legacy_server_role_change_and_no_change(store):
    ctx = make_ctx()
    guild = make_guild()
    legacy = SimpleNamespace(id=400, name="Active Booking", position=1)
    guild.roles = [legacy]
    with patch("bot.services.roles.get_value", AsyncMock(return_value=None)):
        data = await sw.prefilled_data(ctx.db, GUILD, guild)
    assert data["orig_invites_role_id"] == 400 and data["invites_role_id"] == 400
    seed(store, SUMMARY, {**data, "dj_channel": 100, "log_channel": 200, "dj_role_id": 300})
    same = await sw.render_step(ctx, guild, store.drafts[GUILD])
    assert "Warning" not in same.content
    store.drafts[GUILD].data["invites_role_id"] = 555
    changed = await sw.render_step(ctx, guild, store.drafts[GUILD])
    assert "members are not moved to the new role" in changed.content


@pytest.mark.asyncio
async def test_save_failure_keeps_draft_and_reports(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {"rerun": True, **FULL})
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.ui.setup_wizard._write_all", AsyncMock(side_effect=RuntimeError("db"))):
        await sw.save_from_draft(ctx, inter)
    assert GUILD in store.drafts
    inter.followup.send.assert_awaited_once_with(texts.SETUP_ERROR, ephemeral=True)
    inter.edit_original_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_role_reuses_existing_role_without_creating(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_role_id": 300})
    guild = make_guild()
    guild.roles = [SimpleNamespace(id=400, name="Active Booking", position=1)]
    inter = make_inter(guild)
    await sw.SetupWizardView(ctx).on_create_role(inter)
    guild.create_role.assert_not_awaited()
    assert store.drafts[GUILD].data["invites_role_id"] == 400


@pytest.mark.asyncio
async def test_roles_check_rejects_everyone_managed_and_same(store):
    ctx = make_ctx()
    guild = make_guild(role_ids=(300, 400, 555, GUILD))
    base = {"dj_role_id": 300, "invites_role_id": 400}
    for data, needle in (
        ({"dj_role_id": 300, "invites_role_id": 300}, "two different roles"),
        ({"dj_role_id": GUILD, "invites_role_id": 400}, "@everyone"),
    ):
        seed(store, 3, data)
        view = await sw.render_step(ctx, guild, store.drafts[GUILD])
        assert view.next_disabled and needle in view.content
    managed = make_guild()
    original = managed.get_role
    managed.get_role = lambda rid: (
        SimpleNamespace(**{**vars(original(rid)), "managed": rid == 400}) if original(rid) else None
    )
    seed(store, 3, base)
    view = await sw.render_step(ctx, managed, store.drafts[GUILD])
    assert view.next_disabled and "integration" in view.content


@pytest.mark.asyncio
async def test_first_run_prefills_stored_values_but_is_not_a_rerun(store):
    from bot.cogs.setup import SetupCog

    store.config[(GUILD, "slots_per_event")] = "4"
    store.config[(GUILD, "outreach_days_before")] = "9"
    ctx = make_ctx()
    cog = SetupCog(ctx)
    inter = make_inter(make_guild())
    inter.edit_original_message = AsyncMock(return_value=SimpleNamespace(id=777))
    await cog.setup.callback(cog, inter)
    d = store.drafts[GUILD].data
    assert d["rerun"] is False
    assert d["slots_per_event"] == 4 and d["outreach_days_before"] == 9


@pytest.mark.asyncio
async def test_prefill_caps_slots_and_uses_parse_hhmm(store):
    ctx = make_ctx()
    store.config[(GUILD, "slots_per_event")] = "500"
    store.config[(GUILD, "default_start_time")] = "7:05"
    data = await sw.prefilled_data(ctx.db, GUILD)
    assert data["slots_per_event"] == 6 and data["default_start_time"] == "07:05"
    store.config[(GUILD, "default_start_time")] = "99:99"
    assert (await sw.prefilled_data(ctx.db, GUILD))["default_start_time"] == "20:00"


def test_parse_hhmm():
    assert sw.parse_hhmm("9:30") == "09:30" and sw.parse_hhmm("23:59") == "23:59"
    assert sw.parse_hhmm("24:00") is None and sw.parse_hhmm("x") is None


# ---------- final fix wave ----------

@pytest.mark.asyncio
async def test_failed_activation_after_channels_leaves_guild_idle(store):
    ctx = make_ctx()
    seed(store, SUMMARY, FULL)
    inter = make_inter(make_guild())
    await inter.response.defer()
    real_set = store.set_value

    async def failing_set(db, guild_id, key, value):
        if key == "dj_role_id":
            raise RuntimeError("db down")
        await real_set(db, guild_id, key, value)

    with patch("bot.ui.setup_wizard.db_config.set_value", failing_set), patch(
        "bot.services.activation.get_value", store.get_value
    ):
        await sw.activate_from_draft(ctx, inter)
        assert store.config[(GUILD, "dj_channel")] == "100"  # channels were written
        assert store.config[(GUILD, "bot_active")] == "false"
        assert await REAL_IS_ACTIVE(ctx.db, GUILD) is False
    assert GUILD in store.drafts


@pytest.mark.asyncio
async def test_activation_writes_false_flag_before_any_other_key(store):
    ctx = make_ctx()
    seed(store, SUMMARY, FULL)
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=0)):
        await sw.activate_from_draft(ctx, inter)
    assert store.writes[0] == ("bot_active", "false")
    assert store.writes[-1] == ("bot_active", "true")


@pytest.mark.asyncio
async def test_activate_loads_events_inside_replenish_not_before(store):
    ctx = make_ctx()
    seed(store, SUMMARY, FULL)
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=2)) as rep:
        await sw.activate_from_draft(ctx, inter)
    assert rep.await_args.args == (ctx.db, ctx.notion_client, GUILD)  # no stale list passed
    # the lineup refresh uses a list loaded after replenishing
    ctx.lineup_service.refresh.assert_awaited_once()


def test_invites_caps_name_field_and_range():
    _, errors = sw.parse_invites_fields({"days": "366", "hours": "721"})
    assert len(errors) == 2
    assert "Days before an event" in errors[0] and "1 to 365" in errors[0]
    assert "Hours between invites" in errors[1] and "1 to 720" in errors[1]
    ok, none = sw.parse_invites_fields({"days": "365", "hours": "720"})
    assert none == [] and ok == {"outreach_days_before": 365, "outreach_cooldown_hours": 720}


@pytest.mark.asyncio
async def test_concurrent_selects_do_not_lose_a_choice(store):
    ctx = make_ctx()
    seed(store, 1, {})
    real_get, real_save = store.get_draft, store.save_draft

    async def slow_get(*a):
        await asyncio.sleep(0)
        return await real_get(*a)

    async def slow_save(*a):
        await asyncio.sleep(0)
        await real_save(*a)

    guild = make_guild()
    view = sw.SetupWizardView(ctx)
    with patch("bot.ui.setup_wizard.get_draft", slow_get), patch(
        "bot.ui.setup_wizard.save_draft", slow_save
    ):
        await asyncio.gather(
            view._choose_channel(make_inter(guild, values=["100"]), "dj_channel"),
            view._choose_channel(make_inter(guild, values=["200"]), "log_channel"),
        )
    assert store.drafts[GUILD].data == {"dj_channel": 100, "log_channel": 200}


def test_zone_abbr_and_labels_follow_the_zone():
    assert sw.zone_abbr("Europe/Berlin") in ("CET", "CEST")
    assert sw.zone_abbr("Asia/Tokyo") == "JST"
    assert sw.zone_abbr("Nope/Zone") == "Nope/Zone"


@pytest.mark.asyncio
async def test_start_time_step_uses_zone_abbreviation(store):
    ctx = make_ctx()
    seed(store, STEP["start_time"], {"timezone": "Asia/Tokyo"})
    step = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    assert "JST" in step.content and "CET" not in step.content


# ---------- schedule switch ----------

OFF = {"event_schedule_enabled": False}
HIDDEN = ("interval", "first_date", "lookahead")


@pytest.mark.asyncio
async def test_schedule_step_toggle_flips_draft_value(store):
    ctx = make_ctx()
    seed(store, STEP["schedule"], {})
    inter = make_inter(make_guild())
    await sw.SetupWizardView(ctx).on_schedule_toggle(inter)
    assert store.drafts[GUILD].data["event_schedule_enabled"] is False
    assert "Scheduled events: off" in last_content(inter)
    toggle = next(c for c in last_view(inter).children if c.custom_id == sw.ID_SCHEDULE_TOGGLE)
    assert toggle.label == "Turn on"
    inter = make_inter(make_guild())
    await sw.SetupWizardView(ctx).on_schedule_toggle(inter)
    assert store.drafts[GUILD].data["event_schedule_enabled"] is True
    assert store.writes == []


@pytest.mark.asyncio
async def test_schedule_toggle_refuses_non_admin(store):
    view = sw.SetupWizardView(make_ctx())
    assert await view.interaction_check(make_inter(make_guild(), user=999)) is False


@pytest.mark.asyncio
async def test_next_skips_scheduled_steps_when_off_and_back_in_reverse(store):
    ctx = make_ctx()
    guild = make_guild()
    seed(store, STEP["schedule"], {**FULL, **OFF})
    view = sw.SetupWizardView(ctx)
    await view._navigate(make_inter(guild), 1)
    assert store.drafts[GUILD].step == STEP["timezone"]
    await view._navigate(make_inter(guild), -1)
    assert store.drafts[GUILD].step == STEP["schedule"]
    await view._navigate(make_inter(guild), -1)
    assert store.drafts[GUILD].step == STEP["slot_length"]


@pytest.mark.asyncio
async def test_next_visits_scheduled_steps_when_on(store):
    ctx = make_ctx()
    guild = make_guild()
    seed(store, STEP["schedule"], {**FULL})
    view = sw.SetupWizardView(ctx)
    visited = []
    for _ in range(4):
        await view._navigate(make_inter(guild), 1)
        visited.append(sw.STEPS[store.drafts[GUILD].step])
    assert visited == ["interval", "first_date", "lookahead", "timezone"]


def test_next_step_clamps_at_both_ends():
    assert sw.next_step({}, 0, -1) == 0
    assert sw.next_step({}, len(sw.STEPS) - 1, 1) == len(sw.STEPS) - 1
    assert sw.scheduled_step_keys() == HIDDEN


@pytest.mark.asyncio
@pytest.mark.parametrize(("data", "total"), [({}, 13), (OFF, 10)])
async def test_step_numbers_are_consecutive(store, data, total):
    ctx = make_ctx()
    guild = make_guild()
    numbers = []
    for key in sw.STEPS:
        if key in HIDDEN and data == OFF:
            continue
        seed(store, STEP[key], {**FULL, **data})
        step = await sw.render_step(ctx, guild, store.drafts[GUILD])
        found = re.search(r"Step (\d+) of (\d+)", step.content)
        assert found and int(found.group(2)) == total, key
        numbers.append(int(found.group(1)))
    # the summary page belongs to the last numbered step
    assert numbers == [*range(1, total + 1), total]


@pytest.mark.asyncio
async def test_summary_shows_scheduled_off(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {**FULL, **OFF})
    view = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    assert "Scheduled events: off" in view.content
    assert "every" not in view.content.split("Scheduled events")[1].splitlines()[0]


@pytest.mark.asyncio
async def test_summary_shows_first_date_when_set(store):
    ctx = make_ctx()
    seed(store, SUMMARY, {**FULL, "event_interval_days": 14, "first_event_date": "2099-01-02"})
    view = await sw.render_step(ctx, make_guild(), store.drafts[GUILD])
    assert "Scheduled events: every 14 days from 2099-01-02, 6 ahead" in view.content


@pytest.mark.asyncio
async def test_prefill_defaults_schedule_on_and_drops_past_first_date(store):
    ctx = make_ctx()
    data = await sw.prefilled_data(ctx.db, GUILD)
    assert data["event_schedule_enabled"] is True and data["first_event_date"] == ""
    store.config[(GUILD, "event_schedule_enabled")] = "false"
    store.config[(GUILD, "first_event_date")] = "2099-01-02"
    data = await sw.prefilled_data(ctx.db, GUILD)
    assert data["event_schedule_enabled"] is False and data["first_event_date"] == "2099-01-02"
    store.config[(GUILD, "first_event_date")] = "2001-01-02"
    assert (await sw.prefilled_data(ctx.db, GUILD))["first_event_date"] == ""
    store.config[(GUILD, "first_event_date")] = "garbage"
    assert (await sw.prefilled_data(ctx.db, GUILD))["first_event_date"] == ""


@pytest.mark.asyncio
async def test_activate_with_schedule_off_creates_no_events_and_posts_lineup(store):
    ctx = make_ctx()
    ctx.notion_client.create_event = AsyncMock()
    seed(store, SUMMARY, {**FULL, **OFF, "first_event_date": "2099-01-02"})
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.services.event_replenish.get_value", store.get_value), patch(
        "bot.services.event_replenish.get_manual_page_ids", AsyncMock(return_value=set())
    ):
        await sw.activate_from_draft(ctx, inter)
    assert store.config[(GUILD, "event_schedule_enabled")] == "false"
    assert store.config[(GUILD, "first_event_date")] == "2099-01-02"
    ctx.notion_client.create_event.assert_not_awaited()
    ctx.lineup_service.refresh.assert_awaited_once()
    assert store.config[(GUILD, "bot_active")] == "true"
