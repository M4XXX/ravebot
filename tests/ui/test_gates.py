"""Tests for bot/ui/gates.py and the idle behaviour of the DJ-facing views."""
import inspect
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.services import activation
from bot.ui import dj_profile, views
from bot.ui.gates import ADMIN_IDLE_REPLY, IDLE_REPLY, GatedView
from bot.ui.log_reply import LogReplyView

DJ_VIEWS = [
    dj_profile.OnboardingIntroView,
    dj_profile.QuestionView,
    dj_profile.SummaryView,
    dj_profile.OverviewView,
    views.FallbackMenuView,
    views.LineupEventPickView,
    views.LineupPostView,
    views.EventSelectionView,
    views.SlotSelectionView,
    views.BookingConfirmView,
    views.CancellationView,
    views.CancelConfirmView,
    views.OutreachView,
]


def _idle(monkeypatch) -> None:
    monkeypatch.setattr(activation, "bot_is_active", AsyncMock(return_value=False))


def _inter() -> MagicMock:
    inter = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    return inter


def test_idle_texts() -> None:
    assert IDLE_REPLY == "I can't answer right now. Please try again later."
    assert ADMIN_IDLE_REPLY == "The bot is not active yet. Run /setup to finish the setup."


def test_every_dj_view_is_gated_and_log_reply_is_not() -> None:
    for cls in DJ_VIEWS:
        assert issubclass(cls, GatedView), cls.__name__
    assert not issubclass(LogReplyView, GatedView)
    # no other View subclass in views.py bypasses the gate
    for module in (views, dj_profile):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, disnake.ui.View) and cls.__module__ == module.__name__:
                assert issubclass(cls, GatedView), cls.__name__


@pytest.mark.asyncio
async def test_idle_blocks_with_private_reply(monkeypatch) -> None:
    _idle(monkeypatch)
    view = views.LineupEventPickView([], MagicMock())
    inter = _inter()
    assert await view.interaction_check(inter) is False
    inter.response.send_message.assert_awaited_once_with(IDLE_REPLY, ephemeral=True)


@pytest.mark.asyncio
async def test_active_lets_interaction_through(monkeypatch) -> None:
    view = views.LineupEventPickView([], MagicMock())
    inter = _inter()
    assert await view.interaction_check(inter) is True
    inter.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_reads_db_from_view_ctx(monkeypatch) -> None:
    seen = AsyncMock(return_value=True)
    monkeypatch.setattr(activation, "bot_is_active", seen)
    ctx = MagicMock()
    view = views.FallbackMenuView(ctx, None)
    inter = _inter()
    await view.interaction_check(inter)
    assert seen.await_args.args[1] is ctx.db


def _build(cls):
    ctx = MagicMock()
    if cls is views.LineupEventPickView:
        return cls([], ctx)
    if cls is views.FallbackMenuView:
        return cls(ctx, None)
    return MagicMock(spec=cls, ctx=ctx, _ctx=ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", DJ_VIEWS)
async def test_each_dj_view_blocks_idle_and_passes_active(cls, monkeypatch) -> None:
    view = _build(cls)
    check = GatedView.interaction_check
    inter = _inter()
    assert await check(view, inter) is True
    _idle(monkeypatch)
    inter = _inter()
    assert await check(view, inter) is False
    inter.response.send_message.assert_awaited_once_with(IDLE_REPLY, ephemeral=True)


@pytest.mark.asyncio
async def test_gate_fails_closed_when_check_raises(monkeypatch) -> None:
    monkeypatch.setattr(activation, "bot_is_active", AsyncMock(side_effect=RuntimeError("db")))
    view = views.LineupEventPickView([], MagicMock())
    inter = _inter()
    assert await view.interaction_check(inter) is False
    inter.response.send_message.assert_awaited_once_with(IDLE_REPLY, ephemeral=True)


# --- maintenance gate ---


def _flow_inter(user_id: int = 1) -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.id = user_id
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.followup.send = AsyncMock()
    return inter


async def test_refuse_in_maintenance_only_while_set() -> None:
    from bot.ui import texts
    from bot.ui.gates import refuse_in_maintenance
    from tests.conftest import make_real_ctx

    ctx = make_real_ctx(db=MagicMock())
    inter = _flow_inter()
    assert await refuse_in_maintenance(inter, ctx) is False
    inter.response.send_message.assert_not_awaited()
    ctx.maintenance = texts.MAINTENANCE
    assert await refuse_in_maintenance(inter, ctx) is True
    inter.response.send_message.assert_awaited_once_with(texts.MAINTENANCE, ephemeral=True)


async def test_booking_refused_in_maintenance(monkeypatch) -> None:
    from bot.db import bookings
    from bot.ui import texts
    from tests.conftest import make_real_ctx
    from tests.notion.factories import make_event

    acquire = AsyncMock(return_value=True)
    monkeypatch.setattr(bookings, "acquire_slot_lock", acquire)
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.maintenance = texts.MAINTENANCE
    lock = ctx.get_user_lock(1)
    await lock.acquire()
    view = views.BookingConfirmView(ctx, make_event(page_id="e1"), 0, 1, lock, 1)
    inter = _flow_inter()
    await view._handle_confirm(inter)
    inter.response.send_message.assert_awaited_once_with(texts.MAINTENANCE, ephemeral=True)
    acquire.assert_not_awaited()
    assert not lock.locked()


async def test_cancel_refused_in_maintenance(monkeypatch) -> None:
    from bot.db import bookings
    from bot.ui import texts
    from tests.conftest import make_real_ctx
    from tests.notion.factories import make_event

    release = AsyncMock(return_value=True)
    monkeypatch.setattr(bookings, "release_slot_lock", release)
    ctx = make_real_ctx(db=MagicMock())
    ctx.maintenance = texts.MAINTENANCE
    lock = ctx.get_user_lock(1)
    await lock.acquire()
    view = views.CancelConfirmView(ctx, make_event(page_id="e1"), 0, 1, lock, 1)
    inter = _flow_inter()
    await view._handle_confirm(inter)
    inter.response.send_message.assert_awaited_once_with(texts.MAINTENANCE, ephemeral=True)
    release.assert_not_awaited()
    assert not lock.locked()


async def test_profile_edit_refused_in_maintenance() -> None:
    from bot.ui import texts
    from tests.conftest import make_real_ctx

    ctx = make_real_ctx(db=MagicMock())
    ctx.maintenance = texts.MAINTENANCE
    ctx.dj_conversations = MagicMock()
    ctx.dj_conversations.start_edit = AsyncMock()
    view = dj_profile.OverviewView(ctx, in_group=True)
    inter = _flow_inter()
    await view._edit(inter, "name")
    inter.response.send_message.assert_awaited_once_with(texts.MAINTENANCE, ephemeral=True)
    ctx.dj_conversations.start_edit.assert_not_awaited()
