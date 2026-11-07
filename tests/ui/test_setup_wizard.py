"""Tests for the setup wizard view (bot/ui/setup_wizard.py)."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db.setup_drafts import SetupDraft
from bot.ui import setup_wizard as sw
from bot.ui import texts

GUILD = 10
ADMIN = 1


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
    ctx.db = MagicMock()
    ctx.db.fetchval = AsyncMock(return_value=1)
    ctx.notion_client.list_events = AsyncMock(return_value=events or [])
    ctx.lineup_service.refresh = AsyncMock(return_value=True)
    return ctx


def make_guild(perms=None, channel_ids=(100, 200)):
    perms = perms or {"send_messages": True, "embed_links": True}
    guild = MagicMock()
    guild.id = GUILD

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
    assert {c.custom_id for c in view.children} == {
        sw.ID_BACK, sw.ID_NEXT, sw.ID_DJ_CHANNEL, sw.ID_LOG_CHANNEL,
        sw.ID_ACTIVATE, sw.ID_SAVE, sw.ID_CHANGE,
    }
    for _ in view.children:
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
    inter = make_inter(make_guild(), values=[SimpleNamespace(id=100)])
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
    seed(store, 2, {"dj_channel": 100, "log_channel": 200})
    guild = make_guild(perms)
    inter = make_inter(guild, values=[SimpleNamespace(id=200)])
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
    store.drafts[GUILD].data.update(dj_channel=100, log_channel=200)
    store.drafts[GUILD].step = 2
    inter = make_inter(guild)
    await view._navigate(inter, 1)
    assert store.drafts[GUILD].step == 3
    assert ids(last_view(inter)) == {sw.ID_BACK, sw.ID_ACTIVATE}
    assert "<#100>" in last_content(inter) and "<#200>" in last_content(inter)
    inter = make_inter(guild)
    await view._navigate(inter, -1)
    assert store.drafts[GUILD].step == 2
    assert store.writes == []


def test_total_steps_follows_step_list():
    assert sw.total_steps() == len(sw.STEPS) - 1
    assert sw.STEPS[-1] == "summary"


# ---------- activation ----------

def _event(date):
    return SimpleNamespace(date=date)


@pytest.mark.asyncio
async def test_activate_writes_keys_flag_and_runs_effects_once_on_double_press(store):
    ctx = make_ctx(events=[_event("e")])
    seed(store, 3, {"dj_channel": 100, "log_channel": 200})
    store.config[(GUILD, "slots_per_event")] = "4"  # stored value must survive
    i1, i2 = make_inter(make_guild()), make_inter(make_guild())
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=6)) as rep:
        await i1.response.defer()
        await i2.response.defer()
        await asyncio.gather(sw.activate_from_draft(ctx, i1), sw.activate_from_draft(ctx, i2))
    assert rep.await_count == 1
    assert rep.await_args.args[2] == GUILD
    assert ctx.lineup_service.refresh.await_count == 1
    c = {k: v for (g, k), v in store.config.items()}
    assert c["dj_channel"] == "100" and c["log_channel"] == "200"
    assert c["bot_active"] == "true"
    assert c["slots_per_event"] == "4"
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
    seed(store, 3, {"dj_channel": 100, "log_channel": 200})
    inter = make_inter(make_guild())
    view = sw.SetupWizardView(ctx)
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=0)):
        await view.activate.callback(inter)
    inter.response.defer.assert_awaited_once()
    assert store.config[(GUILD, "bot_active")] == "true"


@pytest.mark.asyncio
async def test_activate_with_missing_channel_writes_nothing(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_channel": 100})
    inter = make_inter(make_guild())
    await inter.response.defer()
    await sw.activate_from_draft(ctx, inter)
    assert store.writes == []
    assert GUILD in store.drafts
    inter.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_failure_keeps_draft_for_retry(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_channel": 100, "log_channel": 200})
    inter = make_inter(make_guild())
    await inter.response.defer()
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(side_effect=RuntimeError("x"))):
        await sw.activate_from_draft(ctx, inter)
    assert GUILD in store.drafts
    inter.followup.send.assert_awaited_once_with(texts.SETUP_ERROR, ephemeral=True)


@pytest.mark.asyncio
async def test_activate_on_foreign_message_is_refused(store):
    ctx = make_ctx()
    seed(store, 3, {"dj_channel": 100, "log_channel": 200}, message_id=55)
    inter = make_inter(make_guild(), message_id=99)
    await sw.activate_from_draft(ctx, inter)
    assert store.writes == []


@pytest.mark.asyncio
async def test_restart_rebuilds_view_from_stored_draft(store):
    guild = make_guild()
    seed(store, 1, {})
    # first process: choose the DJ channel
    ctx1 = make_ctx()
    inter = make_inter(guild, values=[SimpleNamespace(id=100)])
    await sw.SetupWizardView(ctx1)._choose_channel(inter, "dj_channel")
    # restart: a new view and ctx know nothing but the stored draft
    ctx2 = make_ctx()
    view2 = sw.SetupWizardView(ctx2)
    inter = make_inter(guild)
    await view2._navigate(inter, 1)
    assert store.drafts[GUILD].step == 2
    inter = make_inter(guild, values=[SimpleNamespace(id=200)])
    await view2._choose_channel(inter, "log_channel")
    await view2._navigate(make_inter(guild), 1)
    assert store.drafts[GUILD].step == 3
    inter = make_inter(guild)
    with patch("bot.ui.setup_wizard.replenish_events", AsyncMock(return_value=0)):
        await sw.activate_from_draft(ctx2, inter)
    assert store.config[(GUILD, "dj_channel")] == "100"
    assert store.config[(GUILD, "log_channel")] == "200"


# ---------- change settings and save ----------

@pytest.mark.asyncio
async def test_change_settings_prefills_from_stored_values_and_save_keeps_events(store):
    ctx = make_ctx()
    store.config[(GUILD, "dj_channel")] = "100"
    store.config[(GUILD, "log_channel")] = "200"
    store.config[(GUILD, "bot_active")] = "true"
    guild = make_guild()
    inter = make_inter(guild)
    view = sw.SetupWizardView(ctx)
    await view.change.callback(inter)
    draft = store.drafts[GUILD]
    assert draft.data == {"rerun": True, "dj_channel": 100, "log_channel": 200}
    assert draft.message_id == 55 and draft.step == 0
    store.writes.clear()
    draft.step = 3
    inter = make_inter(guild)
    await view.save.callback(inter)
    summary = await sw.render_step(ctx, guild, draft)
    assert ids(sw.SetupWizardView(ctx, summary)) == {sw.ID_BACK, sw.ID_SAVE}
    ctx.lineup_service.refresh.assert_not_awaited()
    assert GUILD not in store.drafts
    assert texts.SETUP_SAVED == last_content(inter)


@pytest.mark.asyncio
async def test_activate_refuses_rerun_draft(store):
    ctx = make_ctx()
    seed(store, 3, {"rerun": True, "dj_channel": 100, "log_channel": 200})
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
        texts.setup_active_message(3, "<#1>"),
    ]
    for s in strings:
        assert "—" not in s
        assert not re.search(r"\bUTC\b", s)
