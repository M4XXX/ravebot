"""Exact catalog strings in the booking and cancel flows (Task 8)."""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import disnake

from bot.ui import texts
from bot.ui.embeds import BookedEmbed, BookingConfirmEmbed
from bot.ui.views import (
    BookingConfirmView,
    CancelConfirmView,
    CancellationView,
    EventSelectionView,
    FallbackMenuView,
    SlotSelectionView,
    prepare_booking,
)
from tests.conftest import make_real_ctx
from tests.notion.factories import make_event, make_slot


def _event(booked: int = 0, slots: int = 4):
    return make_event(
        event_id="Rave One",
        page_id="evt-1",
        date=datetime.date(2026, 11, 14),
        start=datetime.time(20, 0),
        slot_count=slots,
        slots=[make_slot(i + 1, dj_page_id=f"dj-{i}", dj_name=f"DJ{i}") for i in range(booked)],
    )


def _inter() -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 1
    inter.author.display_name = "DJ"
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    # answers after a defer go through the followup; tests read both the same way
    inter.followup.send = inter.response.send_message
    return inter


def _msg(inter: MagicMock) -> str:
    return inter.response.send_message.call_args[0][0]


def _labels(view) -> list[str]:
    return [c.label for c in view.children]


async def _locked(ctx, uid):
    lock = ctx.get_user_lock(uid)
    await lock.acquire()
    return lock


def test_confirm_embed_texts() -> None:
    event = _event()
    embed = BookingConfirmEmbed(event, 1)
    assert embed.title == "Confirm your booking"
    assert embed.description == "Rave One\nSaturday 14.11.2026, slot 2 at 21:00"
    assert "<t:" not in embed.description
    assert not embed.fields


def test_booked_embed_texts() -> None:
    event = _event()
    embed = BookedEmbed(event, 0)
    assert embed.title == "You're booked"
    assert embed.description == (
        "Rave One\nSaturday 14.11.2026, slot 1 at 20:00\nI'll remind you the day before."
    )
    assert "<t:" not in embed.description


async def test_prepare_booking_event_list_and_errors() -> None:
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[_event(booked=1)])
    start = await prepare_booking(ctx, 5)
    assert start.content == ""
    assert start.embed.title == "Which event do you want to play?"
    assert start.embed.description == (
        "**Rave One · Saturday 14.11.2026**\n"
        "🎧 3 of 4 slots open · starts 20:00"
    )
    assert _labels(start.view)[-1] == "Never mind"
    # flow already open
    again = await prepare_booking(ctx, 5)
    assert again.error == texts.FLOW_OPEN == (
        "You already have a booking open. Finish it or press Never mind. "
        "It closes by itself after 15 minutes."
    )
    # no events / all booked / error
    ctx2 = make_real_ctx(db=MagicMock())
    ctx2.notion_client = MagicMock()
    ctx2.notion_client.list_events = AsyncMock(return_value=[])
    assert (await prepare_booking(ctx2, 6)).error == "There are no upcoming events yet."
    ctx2.notion_client.list_events = AsyncMock(return_value=[_event(booked=4)])
    assert (await prepare_booking(ctx2, 6)).error == (
        "All slots are booked. I'll message you if one opens up."
    )
    ctx2.notion_client.list_events = AsyncMock(side_effect=RuntimeError("x"))
    assert (await prepare_booking(ctx2, 6)).error == texts.GENERIC_ERROR


async def test_never_mind_texts_are_booking_aborted() -> None:
    ctx = make_real_ctx(db=MagicMock())
    event = _event()
    for view in (
        EventSelectionView(ctx, [event], await _locked(ctx, 10), 10),
        SlotSelectionView(ctx, event, [0], await _locked(ctx, 11), 11),
        BookingConfirmView(ctx, event, 0, 1, await _locked(ctx, 12), 12),
    ):
        inter = _inter()
        await view._handle_cancel(inter)
        assert _msg(inter) == "No problem. Nothing was booked."


async def test_confirm_buttons() -> None:
    ctx = make_real_ctx(db=MagicMock())
    view = BookingConfirmView(ctx, _event(), 0, 1, ctx.get_user_lock(1), 1)
    assert _labels(view) == ["Confirm", "Back"]


async def test_slot_selection_presents_confirm_embed() -> None:
    ctx = make_real_ctx(db=MagicMock())
    event = _event()
    view = SlotSelectionView(ctx, event, [0, 1], await _locked(ctx, 13), 13)
    inter = _inter()
    await view._handle_slot(inter, 1)
    embed = inter.response.send_message.call_args.kwargs["embed"]
    assert isinstance(embed, BookingConfirmEmbed)
    assert embed.description == "Rave One\nSaturday 14.11.2026, slot 2 at 21:00"


async def _confirm(ctx, uid, *, acquire=True, assign=True, ensure="dj-me", event=None):
    lock = await _locked(ctx, uid)
    view = BookingConfirmView(ctx, event or _event(), 1, 1, lock, uid)
    inter = _inter()
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=acquire)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value=ensure)):
        ctx.notion_client.assign_slot = AsyncMock(return_value=assign)
        await view._handle_confirm(inter)
    return inter, lock


def _ctx_with_notion(events=None):
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=events or [])
    ctx.log_service = None
    ctx.lineup_service = None
    return ctx


async def test_booked_success_sends_booked_embed() -> None:
    ctx = _ctx_with_notion()
    inter, lock = await _confirm(ctx, 20)
    embed = inter.response.send_message.call_args.kwargs["embed"]
    assert isinstance(embed, BookedEmbed)
    assert embed.title == "You're booked"
    assert embed.description.endswith("I'll remind you the day before.")
    assert not lock.locked()


async def test_slot_taken_lists_remaining() -> None:
    event = _event()
    ctx = _ctx_with_notion([event])
    inter, _ = await _confirm(ctx, 21, acquire=False, event=event)
    assert _msg(inter) == (
        "That slot was just taken by someone else. These slots are still open:"
    )
    assert isinstance(inter.response.send_message.call_args.kwargs["view"], SlotSelectionView)


async def test_event_full_when_nothing_remains() -> None:
    event = _event(booked=4)
    ctx = _ctx_with_notion([event])
    inter, lock = await _confirm(ctx, 22, acquire=False, event=event)
    assert _msg(inter) == (
        "All slots of this event are now booked. I'll message you if one opens up."
    )
    assert not lock.locked()


async def test_booking_confirm_generic_errors() -> None:
    # db None
    ctx = make_real_ctx(db=None)
    view = BookingConfirmView(ctx, _event(), 0, 1, await _locked(ctx, 23), 23)
    inter = _inter()
    await view._handle_confirm(inter)
    assert _msg(inter) == texts.GENERIC_ERROR
    # notion None
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    view = BookingConfirmView(ctx, _event(), 0, 1, await _locked(ctx, 24), 24)
    inter = _inter()
    await view._handle_confirm(inter)
    assert _msg(inter) == texts.GENERIC_ERROR
    # acquire raises
    ctx = _ctx_with_notion()
    view = BookingConfirmView(ctx, _event(), 0, 1, await _locked(ctx, 25), 25)
    inter = _inter()
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(side_effect=RuntimeError)):
        await view._handle_confirm(inter)
    assert _msg(inter) == texts.GENERIC_ERROR
    # no dj page
    inter, _ = await _confirm(_ctx_with_notion(), 26, ensure=None)
    assert _msg(inter) == texts.GENERIC_ERROR


def _booking(event, idx=1):
    return {"event": event, "slot_index": idx, "slot_unix": 1}


async def test_cancel_slot_entry_texts() -> None:
    event = _event(booked=2)
    # prompt and view
    ctx = _ctx_with_notion([event])
    inter = _inter()
    rows = [{"event_id": "evt-1", "slot_index": 1}]
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=rows)):
        await FallbackMenuView(ctx).cancel_slot.callback(inter)
    assert _msg(inter) == "Which booking do you want to cancel?"
    view = inter.response.send_message.call_args.kwargs["view"]
    assert _labels(view) == ["Rave One · Slot 2 · 21:00", "Never mind"]
    # no bookings
    ctx = _ctx_with_notion([event])
    inter = _inter()
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=[])):
        await FallbackMenuView(ctx).cancel_slot.callback(inter)
    assert _msg(inter) == "You have no upcoming bookings."
    # flow open
    ctx = _ctx_with_notion([event])
    await _locked(ctx, 1)
    inter = _inter()
    await FallbackMenuView(ctx).cancel_slot.callback(inter)
    assert _msg(inter) == texts.FLOW_OPEN
    # generic errors
    ctx = make_real_ctx(db=None)
    inter = _inter()
    await FallbackMenuView(ctx).cancel_slot.callback(inter)
    assert _msg(inter) == texts.GENERIC_ERROR
    ctx = _ctx_with_notion()
    inter = _inter()
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(side_effect=RuntimeError)):
        await FallbackMenuView(ctx).cancel_slot.callback(inter)
    assert _msg(inter) == texts.GENERIC_ERROR
    ctx = _ctx_with_notion()
    ctx.notion_client.list_events = AsyncMock(side_effect=RuntimeError)
    inter = _inter()
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=rows)):
        await FallbackMenuView(ctx).cancel_slot.callback(inter)
    assert _msg(inter) == texts.GENERIC_ERROR


async def test_book_slot_flow_open_text() -> None:
    ctx = _ctx_with_notion([_event()])
    await _locked(ctx, 1)
    inter = _inter()
    await FallbackMenuView(ctx).book_slot.callback(inter)
    assert texts.FLOW_OPEN in str(inter.response.send_message.call_args)


async def test_cancellation_view_never_mind_and_prompt() -> None:
    ctx = make_real_ctx(db=MagicMock())
    event = _event(booked=2)
    lock = await _locked(ctx, 30)
    view = CancellationView(ctx, [_booking(event)], lock, 30)
    inter = _inter()
    await view._handle_never_mind(inter)
    assert _msg(inter) == "Nothing changed. You're still booked."
    assert not lock.locked()
    lock = await _locked(ctx, 31)
    view = CancellationView(ctx, [_booking(event)], lock, 31)
    inter = _inter()
    await view._handle_booking(inter, _booking(event))
    assert _msg(inter) == "Cancel slot 2 (21:00) at Rave One?"
    assert _labels(inter.response.send_message.call_args.kwargs["view"]) == [
        "Yes, cancel",
        "Keep booking",
    ]


async def test_cancel_confirm_keep_and_done() -> None:
    ctx = _ctx_with_notion()
    event = _event(booked=2)
    lock = await _locked(ctx, 32)
    view = CancelConfirmView(ctx, event, 1, 1, lock, 32)
    inter = _inter()
    await view._handle_keep(inter)
    assert _msg(inter) == "Nothing changed. You're still booked."
    lock = await _locked(ctx, 33)
    view = CancelConfirmView(ctx, event, 1, 1, lock, 33)
    inter = _inter()
    ctx.notion_client.clear_slot = AsyncMock(return_value=True)
    with patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="d")), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()):
        await view._handle_confirm(inter)
    assert _msg(inter) == "Your booking is cancelled. The slot is open again."


async def test_cancel_confirm_generic_errors() -> None:
    event = _event(booked=2)
    ctx = make_real_ctx(db=None)
    view = CancelConfirmView(ctx, event, 1, 1, await _locked(ctx, 34), 34)
    inter = _inter()
    await view._handle_confirm(inter)
    assert _msg(inter) == texts.GENERIC_ERROR
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    view = CancelConfirmView(ctx, event, 1, 1, await _locked(ctx, 35), 35)
    inter = _inter()
    await view._handle_confirm(inter)
    assert _msg(inter) == texts.GENERIC_ERROR
    from bot.notion.client import NotionWriteError

    ctx = _ctx_with_notion()
    ctx.notion_client.clear_slot = AsyncMock(side_effect=NotionWriteError("x"))
    view = CancelConfirmView(ctx, event, 1, 1, await _locked(ctx, 36), 36)
    inter = _inter()
    with patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="d")), \
         patch("bot.ui.views.release_slot_lock", new=AsyncMock(), create=True), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()):
        await view._handle_confirm(inter)
    assert _msg(inter) == texts.GENERIC_ERROR


async def test_go_active_inactive_db_none_generic_error() -> None:
    ctx = make_real_ctx(db=None)
    view = FallbackMenuView(ctx)
    for button in (view.go_active, view.go_inactive):
        inter = _inter()
        await button.callback(inter)
        assert _msg(inter) == texts.GENERIC_ERROR
