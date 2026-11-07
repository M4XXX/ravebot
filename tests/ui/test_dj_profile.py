"""Tests for bot/ui/dj_profile.py: the DJ conversation views and their wiring."""
import inspect
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.services import dj_conversation as svc_mod
from bot.services.dj_conversation import DjConversationService, Reply
from bot.ui import dj_profile, texts
from bot.ui.gates import GatedView
from tests.conftest import make_real_ctx
from tests.services.test_dj_conversation import FakeDb

GUILD = 500
A = 1
_ids = itertools.count(1000)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    db = FakeDb()
    for name in (
        "get_conversation",
        "save_conversation",
        "delete_conversation",
        "set_conversation_message",
        "get_dj_by_discord_id",
        "insert_dj",
        "update_dj_fields",
        "dj_name_exists",
        "update_active_booking",
    ):
        monkeypatch.setattr(svc_mod, name, getattr(db, name))
    monkeypatch.setattr(svc_mod, "sync_dj", AsyncMock(return_value=True))
    monkeypatch.setattr(svc_mod, "get_invites_role", AsyncMock(return_value=MagicMock()))
    return db


@pytest.fixture
def member() -> MagicMock:
    m = MagicMock()
    m.display_name = "alice"
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.roles = []
    return m


@pytest.fixture
def guild(member: MagicMock) -> MagicMock:
    g = MagicMock()
    g.id = GUILD
    g.get_member = MagicMock(return_value=member)
    return g


@pytest.fixture
def client(guild: MagicMock) -> MagicMock:
    c = MagicMock()
    c.get_guild = MagicMock(return_value=guild)
    c.guilds = [guild]
    return c


@pytest.fixture
def ctx(fake: FakeDb, client: MagicMock) -> Any:
    c = make_real_ctx(db=MagicMock(name="pool"))
    c.log_service = MagicMock()
    c.log_service.log_new_dj = AsyncMock()
    c.dj_conversations = DjConversationService(c, client=client)
    return c


def make_inter(client: MagicMock, message_id: int | None = None, user: int = A) -> MagicMock:
    inter = MagicMock()
    inter.author.id = user
    inter.message.id = message_id
    inter.client = client
    inter.response.is_done = MagicMock(return_value=False)

    async def _defer(*args: Any, **kwargs: Any) -> None:
        inter.response.is_done.return_value = True

    async def _send_message(*args: Any, **kwargs: Any) -> None:
        inter.response.is_done.return_value = True

    inter.response.defer = AsyncMock(side_effect=_defer)
    inter.response.send_message = AsyncMock(side_effect=_send_message)
    inter.sent_id = next(_ids)
    inter.original_response = AsyncMock(return_value=SimpleNamespace(id=inter.sent_id))
    inter.followup.send = AsyncMock(return_value=SimpleNamespace(id=inter.sent_id))
    return inter


def sent(inter: MagicMock) -> tuple[str, dict[str, Any]]:
    """Text and kwargs of the one message the interaction answered with."""
    calls = inter.followup.send.await_args_list + inter.response.send_message.await_args_list
    assert len(calls) == 1, calls
    call = calls[0]
    text = call.args[0] if call.args else call.kwargs.get("content")
    return text, call.kwargs


def ids(view: disnake.ui.View | None) -> list[str]:
    return [] if view is None else [c.custom_id for c in view.children]


def button(view: disnake.ui.View, custom_id: str) -> Any:
    return next(c for c in view.children if c.custom_id == custom_id)


async def press(view: disnake.ui.View, custom_id: str, inter: MagicMock) -> None:
    await button(view, custom_id).callback(inter)


# --- intro view ---


async def test_go_active_starts_conversation_sends_name_question_and_records_id(
    ctx, fake, client
) -> None:
    view = dj_profile.OnboardingIntroView(ctx)
    inter = make_inter(client, message_id=1)

    await press(view, "onboarding:go_active", inter)

    text, kwargs = sent(inter)
    assert text == texts.Q_NAME
    assert isinstance(kwargs["view"], dj_profile.QuestionView)
    assert ids(kwargs["view"]) == ["dj:cancel"]
    assert kwargs.get("ephemeral") is not True
    conv = fake.convs[A]
    assert (conv.step, conv.guild_id, conv.message_id) == ("name", GUILD, inter.sent_id)


async def test_go_active_twice_gives_the_same_open_question(ctx, fake, client) -> None:
    view = dj_profile.OnboardingIntroView(ctx)
    first, second = make_inter(client, 1), make_inter(client, 1)
    await press(view, "onboarding:go_active", first)
    await press(view, "onboarding:go_active", second)

    assert sent(first)[0] == sent(second)[0] == texts.Q_NAME
    assert fake.convs[A].step == "name"
    assert fake.convs[A].message_id == second.sent_id


async def test_go_active_without_shared_guild_uses_zero(ctx, fake, client) -> None:
    client.guilds = []
    await press(dj_profile.OnboardingIntroView(ctx), "onboarding:go_active", make_inter(client))
    assert fake.convs[A].guild_id == 0


async def test_not_now_sends_declined(ctx, fake, client) -> None:
    inter = make_inter(client, 1)
    await press(dj_profile.OnboardingIntroView(ctx), "onboarding:pass_for_now", inter)
    assert sent(inter)[0] == texts.ONBOARD_DECLINED
    assert A not in fake.convs


# --- stale buttons ---


async def _to_step(ctx, client, step: str) -> int:
    """Start onboarding and answer until `step`; return the open message id."""
    inter = make_inter(client, 1)
    await press(dj_profile.OnboardingIntroView(ctx), "onboarding:go_active", inter)
    message_id = inter.sent_id
    answers = {"name": "Neon Fox", "vrchat": "foxvr", "link": "twitch.tv/neon"}
    service = ctx.dj_conversations
    for current in ("name", "vrchat", "logo", "link", "alt_link"):
        if current == step:
            return message_id
        if current in ("logo", "alt_link"):
            inter = make_inter(client, message_id)
            await press(dj_profile.QuestionView(ctx, skip=True, as_is=False, remove=False),
                        "dj:skip", inter)
        else:
            reply = await service.handle_message(
                SimpleNamespace(author=SimpleNamespace(id=A), content=answers[current],
                                attachments=[])
            )
            inter = make_inter(client)
            await dj_profile.send_reply(ctx, inter, A, reply)
        message_id = inter.sent_id
    return message_id


async def test_skip_on_stale_message_answers_stale_and_changes_nothing(ctx, fake, client) -> None:
    open_id = await _to_step(ctx, client, "vrchat")
    before = (fake.convs[A].step, fake.convs[A].message_id, dict(fake.convs[A].data))
    inter = make_inter(client, message_id=open_id - 1)
    view = dj_profile.QuestionView(ctx, skip=True, as_is=False, remove=False)

    await press(view, "dj:skip", inter)

    text, kwargs = sent(inter)
    assert text == texts.STALE_BUTTON
    assert kwargs.get("view") is None
    assert (fake.convs[A].step, fake.convs[A].message_id, dict(fake.convs[A].data)) == before


async def test_confirm_and_use_as_is_after_cancel_are_stale(ctx, fake, client) -> None:
    summary_id = await _to_step(ctx, client, "summary")
    assert fake.convs[A].step == "summary"
    cancel = make_inter(client, summary_id)
    await press(dj_profile.SummaryView(ctx), "dj:cancel", cancel)
    assert sent(cancel)[0] == texts.ONBOARD_CANCELLED
    assert A not in fake.convs

    confirm = make_inter(client, summary_id)
    await press(dj_profile.SummaryView(ctx), "dj:confirm", confirm)
    as_is = make_inter(client, summary_id)
    await press(dj_profile.QuestionView(ctx, skip=False, as_is=True, remove=False),
                "dj:use_as_is", as_is)

    assert sent(confirm)[0] == texts.STALE_BUTTON
    assert sent(as_is)[0] == texts.STALE_BUTTON
    fake.insert_dj.assert_not_awaited()
    assert A not in fake.convs


async def test_summary_confirm_saves_and_answers_done(ctx, fake, client) -> None:
    summary_id = await _to_step(ctx, client, "summary")
    inter = make_inter(client, summary_id)
    await press(dj_profile.SummaryView(ctx), "dj:confirm", inter)
    assert sent(inter)[0] == texts.ONBOARD_DONE
    fake.insert_dj.assert_awaited_once()


async def test_start_over_goes_back_to_name(ctx, fake, client) -> None:
    summary_id = await _to_step(ctx, client, "summary")
    inter = make_inter(client, summary_id)
    await press(dj_profile.SummaryView(ctx), "dj:start_over", inter)
    assert sent(inter)[0] == texts.Q_NAME
    assert fake.convs[A].step == "name"
    assert fake.convs[A].message_id == inter.sent_id


async def test_summary_message_has_summary_view_and_records_id(ctx, fake, client) -> None:
    summary_id = await _to_step(ctx, client, "summary")
    assert fake.convs[A].message_id == summary_id


async def test_link_check_error_offers_use_as_is_and_it_works(ctx, fake, client) -> None:
    await _to_step(ctx, client, "link")
    reply = await ctx.dj_conversations.handle_message(
        SimpleNamespace(author=SimpleNamespace(id=A), content="example.com/me", attachments=[])
    )
    inter = make_inter(client)
    await dj_profile.send_reply(ctx, inter, A, reply)
    text, kwargs = sent(inter)
    assert ids(kwargs["view"]) == ["dj:use_as_is", "dj:cancel"]
    assert fake.convs[A].message_id == inter.sent_id

    press_inter = make_inter(client, inter.sent_id)
    await press(kwargs["view"], "dj:use_as_is", press_inter)
    assert fake.convs[A].step == "alt_link"
    assert fake.convs[A].data["link"] == "https://example.com/me"


# --- buttons per Reply flags ---


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, ["dj:cancel"]),
        ({"allow_skip": True}, ["dj:skip", "dj:cancel"]),
        ({"allow_as_is": True}, ["dj:use_as_is", "dj:cancel"]),
        ({"allow_remove": True}, ["dj:remove", "dj:cancel"]),
        ({"allow_skip": True, "allow_as_is": True}, ["dj:skip", "dj:use_as_is", "dj:cancel"]),
    ],
)
async def test_question_buttons_follow_reply_flags(ctx, client, flags, expected) -> None:
    for kind in ("question", "error"):
        inter = make_inter(client)
        await dj_profile.send_reply(ctx, inter, A, Reply("q", kind, step="vrchat", **flags))
        assert ids(sent(inter)[1]["view"]) == expected


async def test_question_view_labels(ctx) -> None:
    view = dj_profile.QuestionView(ctx, skip=True, as_is=True, remove=True)
    labels = {c.custom_id: c.label for c in view.children}
    assert labels == {
        "dj:skip": texts.BTN_SKIP,
        "dj:use_as_is": texts.BTN_USE_AS_IS,
        "dj:remove": texts.BTN_REMOVE,
        "dj:cancel": texts.BTN_CANCEL_SETUP,
    }
    assert view.timeout is None


async def test_summary_view_buttons(ctx) -> None:
    view = dj_profile.SummaryView(ctx)
    assert ids(view) == ["dj:confirm", "dj:start_over", "dj:cancel"]
    confirm = button(view, "dj:confirm")
    assert (confirm.label, confirm.style) == (texts.BTN_LOOKS_GOOD, disnake.ButtonStyle.success)


@pytest.mark.parametrize("kind", ["info", "done", "cancelled", "error"])
async def test_plain_replies_have_no_view_and_record_nothing(ctx, fake, client, kind) -> None:
    ctx.dj_conversations.record_message_id = AsyncMock()
    inter = make_inter(client)
    await dj_profile.send_reply(ctx, inter, A, Reply("x", kind))
    text, kwargs = sent(inter)
    assert text == "x"
    assert kwargs.get("view") is None
    ctx.dj_conversations.record_message_id.assert_not_awaited()


# --- overview ---


@pytest.mark.parametrize(("in_group", "group_id"), [(True, "dj:leave"), (False, "dj:join")])
async def test_overview_has_six_buttons_over_two_rows(ctx, in_group, group_id) -> None:
    view = dj_profile.OverviewView(ctx, in_group=in_group)
    assert ids(view) == [
        "dj:edit:name", "dj:edit:vrchat", "dj:edit:logo",
        "dj:edit:link", "dj:edit:alt_link", group_id,
    ]
    rows = [c.row for c in view.children]
    assert rows == [0, 0, 0, 1, 1, 1]
    assert len({"dj:leave", "dj:join"} & set(ids(view))) == 1
    labels = {c.custom_id: c.label for c in view.children}
    assert labels[group_id] == (texts.BTN_INVITES_OFF if in_group else texts.BTN_INVITES_ON)
    assert labels["dj:edit:name"] == texts.BTN_CHANGE_NAME


async def test_overview_reply_sends_logo_file_and_group_button(ctx, fake, client) -> None:
    fake.djs[A] = {
        "dj_name": "Neon Fox", "vrchat_name": None, "logo_url": None,
        "logo_image": b"PNGDATA", "stream_url_primary": "https://twitch.tv/neon",
        "stream_url_backup": None, "active_booking": True,
    }
    ctx.dj_conversations.record_message_id = AsyncMock()
    inter = make_inter(client)
    await dj_profile.send_reply(ctx, inter, A, await ctx.dj_conversations.show_overview(A))
    text, kwargs = sent(inter)
    assert text.startswith(texts.OVERVIEW_TITLE)
    assert isinstance(kwargs["file"], disnake.File)
    assert kwargs["file"].filename == "logo.png"
    assert "dj:leave" in ids(kwargs["view"])
    ctx.dj_conversations.record_message_id.assert_not_awaited()


async def test_overview_leave_and_join_flip_the_button(ctx, fake, client, member) -> None:
    fake.djs[A] = {
        "dj_name": "Neon Fox", "vrchat_name": None, "logo_url": None, "logo_image": None,
        "stream_url_primary": "https://twitch.tv/neon", "stream_url_backup": None,
        "active_booking": True,
    }
    leave = make_inter(client, 5)
    await press(dj_profile.OverviewView(ctx, in_group=True), "dj:leave", leave)
    assert fake.djs[A]["active_booking"] is False
    assert "dj:join" in ids(sent(leave)[1]["view"])
    assert "file" not in sent(leave)[1]

    join = make_inter(client, 5)
    await press(dj_profile.OverviewView(ctx, in_group=False), "dj:join", join)
    assert fake.djs[A]["active_booking"] is True
    assert "dj:leave" in ids(sent(join)[1]["view"])


async def test_overview_edit_button_starts_edit_with_remove_and_cancel(ctx, fake, client) -> None:
    fake.djs[A] = {"dj_name": "Neon Fox", "active_booking": True}
    inter = make_inter(client, 5)
    await press(dj_profile.OverviewView(ctx, in_group=True), "dj:edit:logo", inter)
    text, kwargs = sent(inter)
    assert text == texts.edit_prompt("logo")
    assert ids(kwargs["view"]) == ["dj:remove", "dj:cancel"]
    conv = fake.convs[A]
    assert (conv.kind, conv.step, conv.guild_id, conv.message_id) == (
        "edit", "logo", GUILD, inter.sent_id
    )


async def test_edit_name_question_has_only_cancel(ctx, fake, client) -> None:
    fake.djs[A] = {"dj_name": "Neon Fox", "active_booking": True}
    inter = make_inter(client, 5)
    await press(dj_profile.OverviewView(ctx, in_group=True), "dj:edit:name", inter)
    assert ids(sent(inter)[1]["view"]) == ["dj:cancel"]


# --- restart ---


async def test_conversation_survives_restart_and_old_buttons_work(ctx, fake, client) -> None:
    open_id = await _to_step(ctx, client, "vrchat")
    # Simulated restart: a new service instance on the same stored rows, the views
    # come from the persistent registration.
    ctx.dj_conversations = DjConversationService(ctx, client=client)
    registered = dj_profile.registered_views(ctx)
    question = next(v for v in registered if isinstance(v, dj_profile.QuestionView))

    inter = make_inter(client, open_id)
    await press(question, "dj:skip", inter)

    assert sent(inter)[0] == texts.Q_LOGO
    assert fake.convs[A].step == "logo"
    assert fake.convs[A].data["vrchat"] is None
    assert fake.convs[A].message_id == inter.sent_id


# --- send_reply ---


async def test_send_reply_to_channel_records_summary_id(ctx, fake, client) -> None:
    await _to_step(ctx, client, "summary")
    channel = MagicMock(spec=disnake.DMChannel)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=4242))
    reply = Reply("sum", "summary", step="summary", image=b"img")
    await dj_profile.send_reply(ctx, channel, A, reply)
    kwargs = channel.send.await_args.kwargs
    assert channel.send.await_args.args[0] == "sum"
    assert isinstance(kwargs["view"], dj_profile.SummaryView)
    assert kwargs["file"].filename == "logo.png"
    assert fake.convs[A].message_id == 4242


async def test_send_reply_uses_response_when_not_done(ctx, client) -> None:
    inter = make_inter(client)
    await dj_profile.send_reply(ctx, inter, A, Reply("hi", "info"))
    inter.response.send_message.assert_awaited_once()
    inter.followup.send.assert_not_awaited()


async def test_send_reply_swallows_http_errors(ctx, client) -> None:
    inter = make_inter(client)
    inter.response.send_message = AsyncMock(
        side_effect=disnake.HTTPException(MagicMock(status=500), "boom")
    )
    await dj_profile.send_reply(ctx, inter, A, Reply("q", "question", step="name"))
    channel = MagicMock(spec=disnake.DMChannel)
    channel.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "closed"))
    await dj_profile.send_reply(ctx, channel, A, Reply("q", "question", step="name"))


async def test_send_reply_retries_summary_without_file_and_records_id(ctx, fake, client) -> None:
    await _to_step(ctx, client, "summary")
    channel = MagicMock(spec=disnake.DMChannel)
    channel.send = AsyncMock(side_effect=[
        disnake.HTTPException(MagicMock(status=413), "Request entity too large"),
        SimpleNamespace(id=777),
    ])
    reply = Reply("sum", "summary", step="summary", image=b"img")
    await dj_profile.send_reply(ctx, channel, A, reply)
    assert channel.send.await_count == 2
    first, second = channel.send.await_args_list
    assert "file" in first.kwargs
    assert "file" not in second.kwargs
    assert second.args[0] == "sum"
    assert isinstance(second.kwargs["view"], dj_profile.SummaryView)
    assert fake.convs[A].message_id == 777


async def test_send_reply_retries_interaction_without_file(ctx, fake, client) -> None:
    await _to_step(ctx, client, "summary")
    inter = make_inter(client, 1)
    inter.response.is_done.return_value = True
    inter.followup.send = AsyncMock(side_effect=[
        disnake.HTTPException(MagicMock(status=413), "too large"),
        SimpleNamespace(id=778),
    ])
    reply = Reply("sum", "summary", step="summary", image=b"img")
    await dj_profile.send_reply(ctx, inter, A, reply)
    second = inter.followup.send.await_args_list[1]
    assert "file" not in second.kwargs
    assert isinstance(second.kwargs["view"], dj_profile.SummaryView)
    assert fake.convs[A].message_id == 778


async def test_send_reply_without_file_does_not_retry(ctx, client) -> None:
    channel = MagicMock(spec=disnake.DMChannel)
    channel.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(status=500), "x"))
    await dj_profile.send_reply(ctx, channel, A, Reply("q", "question", step="name"))
    assert channel.send.await_count == 1


async def test_buttons_without_service_answer_generic_error(client) -> None:
    c = make_real_ctx(db=MagicMock())
    inter = make_inter(client, 1)
    await press(dj_profile.OnboardingIntroView(c), "onboarding:go_active", inter)
    assert sent(inter)[0] == texts.GENERIC_ERROR


# --- registration, gating, no popup ---

ALL_IDS = [
    "onboarding:go_active", "onboarding:pass_for_now",
    "dj:skip", "dj:use_as_is", "dj:remove", "dj:cancel",
    "dj:confirm", "dj:start_over",
    "dj:edit:name", "dj:edit:vrchat", "dj:edit:logo", "dj:edit:link", "dj:edit:alt_link",
    "dj:leave", "dj:join",
]


async def test_registered_views_cover_every_custom_id_exactly_once(ctx) -> None:
    views = dj_profile.registered_views(ctx)
    found = [c.custom_id for v in views for c in v.children]
    assert sorted(found) == sorted(ALL_IDS)
    for view in views:
        assert isinstance(view, GatedView)
        assert view.timeout is None
        assert view.is_persistent()
        assert view.ctx is ctx


def test_every_view_in_dj_profile_is_gated() -> None:
    for _, cls in inspect.getmembers(dj_profile, inspect.isclass):
        if issubclass(cls, disnake.ui.View) and cls.__module__ == dj_profile.__name__:
            assert issubclass(cls, GatedView), cls.__name__


def test_no_modal_left_in_onboarding_ui() -> None:
    root = Path(__file__).resolve().parents[2] / "bot" / "ui"
    for name in ("views.py", "dj_profile.py"):
        assert "Modal" not in (root / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize(
    "build",
    [
        lambda c: dj_profile.OnboardingIntroView(c),
        lambda c: dj_profile.QuestionView(c, skip=True, as_is=True, remove=True),
        lambda c: dj_profile.SummaryView(c),
        lambda c: dj_profile.OverviewView(c, in_group=True),
    ],
)
async def test_idle_bot_buttons_answer_idle_reply(build, ctx, client, monkeypatch) -> None:
    from bot.services import activation
    from bot.ui.gates import IDLE_REPLY

    monkeypatch.setattr(activation, "bot_is_active", AsyncMock(return_value=False))
    view = build(ctx)
    inter = make_inter(client, 1)
    assert await view.interaction_check(inter) is False
    inter.response.send_message.assert_awaited_once_with(IDLE_REPLY, ephemeral=True)
