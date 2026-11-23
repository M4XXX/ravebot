"""Flow lock safety of the booking and cancel views (review findings 1-6)."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.ui import texts
from tests.conftest import make_real_ctx
from tests.fake_events import fake_events
from tests.notion.factories import make_event


def _inter(user_id: int = 1) -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.id = user_id
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.followup.send = AsyncMock()
    return inter


async def _flow(ctx, user_id: int = 1):
    from bot.ui.views import EventSelectionView

    lock = ctx.get_user_lock(user_id)
    await lock.acquire()
    return lock, EventSelectionView(ctx, [make_event(page_id="e1")], lock, user_id)


async def test_stale_view_cannot_free_or_drive_a_newer_flow() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, old = await _flow(ctx)
    await old._handle_cancel(_inter())  # flow A ends
    assert not lock.locked()
    await lock.acquire()  # flow B
    await old._handle_cancel(_inter())  # A's Never mind again
    assert lock.locked()  # B keeps its lock
    inter = _inter()
    await old._handle_event(inter, make_event(page_id="e1"))
    assert inter.response.send_message.call_args.args[0] == texts.FLOW_STALE


async def test_custom_ids_are_unique_per_view() -> None:
    from bot.ui.views import BookingConfirmView

    ctx = make_real_ctx(db=MagicMock())
    lock_a = ctx.get_user_lock(1)
    lock_b = ctx.get_user_lock(2)
    await lock_a.acquire()
    await lock_b.acquire()
    event = make_event(page_id="e1")
    a = BookingConfirmView(ctx, event, 0, 1, lock_a, 1)
    b = BookingConfirmView(ctx, event, 0, 1, lock_b, 2)
    ids_a = {c.custom_id for c in a.children}
    ids_b = {c.custom_id for c in b.children}
    assert len(ids_a) == 2 and not ids_a & ids_b


async def test_only_the_owner_may_press_and_idle_frees_the_flow() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx, user_id=7)
    stranger = _inter(user_id=8)
    with patch("bot.ui.gates.activation.bot_is_active", new=AsyncMock(return_value=True)):
        assert await view.interaction_check(stranger) is False
        assert await view.interaction_check(_inter(user_id=7)) is True
    assert "someone else" in stranger.response.send_message.call_args.args[0]
    assert lock.locked()
    with patch("bot.ui.gates.activation.bot_is_active", new=AsyncMock(return_value=False)):
        assert await view.interaction_check(_inter(user_id=7)) is False
    assert not lock.locked()


async def test_timeout_frees_the_flow() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    await view.on_timeout()
    assert not lock.locked()


async def test_second_confirm_press_while_busy_is_refused() -> None:
    from bot.ui.views import BookingConfirmView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(1)
    await lock.acquire()
    view = BookingConfirmView(ctx, make_event(page_id="e1"), 0, 1, lock, 1)
    view._busy = True
    inter = _inter()
    await view._handle_confirm(inter)
    assert inter.response.send_message.call_args.args[0] == texts.FLOW_BUSY
    inter.response.defer.assert_not_awaited()


async def test_book_slot_send_failure_frees_the_flow() -> None:
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    fake_events.list_events = AsyncMock(return_value=[make_event(page_id="e1")])
    inter = _inter(user_id=5)
    inter.followup.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(status=404), "x"))
    with patch("bot.ui.views.get_dj_by_discord_id", new=AsyncMock(return_value={"id": 1})):
        await FallbackMenuView(ctx).book_slot.callback(inter)
    inter.response.defer.assert_awaited_once()
    assert not ctx.get_user_lock(5).locked()


async def test_cancel_slot_send_failure_frees_the_flow() -> None:
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    fake_events.list_events = AsyncMock(return_value=[make_event(page_id="e1")])
    inter = _inter(user_id=6)
    inter.followup.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(status=404), "x"))
    rows = [{"event_id": "e1", "slot_index": 0}]
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=rows)):
        await FallbackMenuView(ctx).cancel_slot.callback(inter)
    assert not ctx.get_user_lock(6).locked()


@pytest.mark.parametrize("fail", ["release"])
async def test_cancel_confirm_db_error_frees_the_flow(fail) -> None:
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.lineup_service = None
    ctx.log_service = None
    lock = ctx.get_user_lock(9)
    await lock.acquire()
    view = CancelConfirmView(ctx, make_event(page_id="e1"), 0, 1, lock, 9)
    inter = _inter(user_id=9)
    with patch("bot.db.bookings.release_slot_lock", new=AsyncMock(side_effect=OSError("db"))):
        await view._handle_confirm(inter)
    assert inter.followup.send.call_args.args[0] == texts.GENERIC_ERROR
    assert not lock.locked()


# --- Expiry: disable the buttons and mark the message -------------------------


def _sent_inter(age_seconds: float = 0.0) -> MagicMock:
    import datetime

    inter = _inter()
    inter.created_at = disnake.utils.utcnow() - datetime.timedelta(seconds=age_seconds)
    inter.edit_original_message = AsyncMock()
    return inter


async def test_expiry_disables_buttons_marks_message_and_frees_flow() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    inter = _sent_inter()
    view.track(inter, None, "Pick an event")
    await view._expire()
    assert not lock.locked()
    assert view.is_finished()
    assert all(item.disabled for item in view.children)
    kwargs = inter.edit_original_message.await_args.kwargs
    assert kwargs["view"] is view
    assert kwargs["content"] == f"Pick an event\n\n{texts.FLOW_EXPIRED}"


async def test_expiry_without_text_shows_only_the_note() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    inter = _sent_inter()
    view.track(inter, None, None)
    await view._expire()
    assert inter.edit_original_message.await_args.kwargs["content"] == texts.FLOW_EXPIRED


async def test_expiry_of_a_used_view_keeps_the_flow_and_the_text() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    inter = _sent_inter()
    view.track(inter, None, "Pick an event")
    await view._handle_event(_inter(), make_event(page_id="e1"))  # moves on to the slots
    await view._expire()
    assert lock.locked()  # the slot view still runs this flow
    kwargs = inter.edit_original_message.await_args.kwargs
    assert "content" not in kwargs
    assert all(item.disabled for item in view.children)


async def test_expiry_runs_once_and_survives_a_failed_edit() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    inter = _sent_inter()
    inter.edit_original_message.side_effect = disnake.HTTPException(MagicMock(), "gone")
    view.track(inter, None, "x")
    await view._expire()
    await view.on_timeout()
    assert inter.edit_original_message.await_count == 1


async def test_expiry_fires_before_the_interaction_token_runs_out() -> None:
    from bot.ui import views

    ctx = make_real_ctx(db=MagicMock())
    _, view = await _flow(ctx)
    with patch.object(views.FlowView, "_expire_after", new=AsyncMock()) as after:
        view.track(_sent_inter(age_seconds=600), None, "x")
        await view._expiry_task
    delay = after.await_args.args[0]
    expected = views.INTERACTION_TOKEN_TTL - 600 - views.EXPIRY_EDIT_MARGIN
    assert expected - 2 <= delay <= expected


async def test_plain_dm_message_expires_after_the_full_time_and_is_edited_directly() -> None:
    from bot.ui import views

    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    sent = MagicMock(spec=disnake.Message)
    sent.edit = AsyncMock()
    inter = _sent_inter(age_seconds=600)
    with patch.object(views.FlowView, "_expire_after", new=AsyncMock()) as after:
        view.track(inter, sent, "Pick")
        await view._expiry_task
    assert after.await_args.args[0] == views.FLOW_VIEW_TIMEOUT
    await view._expire()
    sent.edit.assert_awaited_once()
    inter.edit_original_message.assert_not_awaited()


async def test_expiry_timer_fires_by_itself() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    inter = _sent_inter(age_seconds=20 * 60)  # token already old: expire at once
    view.track(inter, None, "x")
    await view._expiry_task
    assert not lock.locked()
    inter.edit_original_message.assert_awaited_once()


async def test_untracked_view_expires_without_an_edit() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock, view = await _flow(ctx)
    view.track(_inter(), None, "x")  # a mock interaction: no created_at, no timer
    assert view._expiry_task is None
    await view.on_timeout()
    assert not lock.locked()


async def test_menu_follows_never_mind_but_not_an_expired_flow() -> None:
    ctx = make_real_ctx(db=MagicMock())
    ctx.menu_service = MagicMock()
    lock, view = await _flow(ctx)
    await view._handle_cancel(_inter())
    ctx.menu_service.schedule.assert_called_once_with(1)

    ctx.menu_service.reset_mock()
    lock, view = await _flow(ctx)
    view._edit = AsyncMock()
    await view._expire()
    assert not lock.locked()
    ctx.menu_service.schedule.assert_not_called()
