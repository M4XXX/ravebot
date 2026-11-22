"""Booking and cancel never wait for Notion (spec decision 9).

The Notion client here hangs on every call (an asyncio.Event nobody sets). The DJ
still gets the answer at once, and the change is queued for the mirror worker.
"""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.sync import worker as worker_mod
from bot.sync.worker import MirrorWorker
from bot.ui import texts
from tests.conftest import make_real_ctx
from tests.fake_events import fake_events
from tests.notion.factories import make_event, make_slot

USER = 30001


class HangingNotion:
    """Every method call is recorded and never returns."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._never = asyncio.Event()

    def __getattr__(self, name: str):  # noqa: ANN204
        if name.startswith("_"):
            raise AttributeError(name)

        async def _hang(*args: object, **kwargs: object) -> None:
            self.calls.append(name)
            await self._never.wait()

        return _hang


def _event():  # noqa: ANN202
    return make_event(
        event_id="Test Rave",
        page_id="evt-uid",
        date=datetime.date.today() + datetime.timedelta(days=5),
        start=datetime.time(20, 0),
        slot_count=4,
        slots=[make_slot(1, dj_page_id="1", dj_name="DJ 1")],
    )


def _inter() -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.display_name = "Nova"
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.fixture
def queued():
    """Replace the outbox with an in-memory list of (entity, key, op)."""
    rows: list[tuple[str, str, str]] = []

    async def enqueue(db, entity, key, op="upsert", notion_page_id=None):  # noqa: ANN001, ANN202
        rows.append((entity, key, op))

    with patch.object(worker_mod, "outbox", SimpleNamespace(enqueue=enqueue)):
        yield rows


def _ctx_with_hanging_notion() -> tuple[object, HangingNotion]:
    ctx = make_real_ctx(db=MagicMock())
    notion = HangingNotion()
    ctx.notion_client = notion
    ctx.mirror = MirrorWorker(ctx.db, notion, None)  # not started: the queue is checked
    return ctx, notion


async def _settle() -> None:
    if worker_mod._pending:
        await asyncio.gather(*list(worker_mod._pending))


async def test_booking_replies_while_notion_down(queued) -> None:
    from bot.ui.embeds import BookedEmbed
    from bot.ui.views import BookingConfirmView

    ctx, notion = _ctx_with_hanging_notion()
    event = _event()
    fake_events.list_events = AsyncMock(return_value=[event])
    lock = ctx.get_user_lock(USER)
    await lock.acquire()
    view = BookingConfirmView(
        ctx=ctx, event=event, slot_index=2, slot_unix=1751400000, lock=lock, user_id=USER
    )
    inter = _inter()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)) as acq:
        await asyncio.wait_for(view._handle_confirm(inter), timeout=1)
    await _settle()

    acq.assert_awaited_once_with(ctx.db, "evt-uid", 2, USER, max_per_dj=2)  # default rules
    assert isinstance(inter.followup.send.call_args.kwargs.get("embed"), BookedEmbed)
    assert queued == [("slot", "evt-uid:2", "upsert")]
    assert notion.calls == []
    assert not lock.locked()


async def test_booking_works_without_notion(queued) -> None:
    """Notion off (no client, no mirror): the booking is made, nothing is queued."""
    from bot.ui.embeds import BookedEmbed
    from bot.ui.views import BookingConfirmView

    ctx = make_real_ctx(db=MagicMock())
    event = _event()
    fake_events.list_events = AsyncMock(return_value=[event])
    lock = ctx.get_user_lock(USER)
    await lock.acquire()
    view = BookingConfirmView(
        ctx=ctx, event=event, slot_index=1, slot_unix=1751400000, lock=lock, user_id=USER
    )
    inter = _inter()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)):
        await view._handle_confirm(inter)
    await _settle()

    assert isinstance(inter.followup.send.call_args.kwargs.get("embed"), BookedEmbed)
    assert queued == []
    assert not lock.locked()


async def test_cancel_never_calls_notion(queued) -> None:
    from bot.ui.views import CancelConfirmView

    ctx, notion = _ctx_with_hanging_notion()
    event = _event()
    fake_events.list_events = AsyncMock(return_value=[event])
    lock = ctx.get_user_lock(USER)
    await lock.acquire()
    view = CancelConfirmView(
        ctx=ctx, event=event, slot_index=0, slot_unix=1751400000, lock=lock, user_id=USER
    )
    inter = _inter()

    with patch("bot.db.bookings.release_slot_lock", new=AsyncMock(return_value=True)) as rel:
        await asyncio.wait_for(view._handle_confirm(inter), timeout=1)
    await _settle()

    rel.assert_awaited_once_with(ctx.db, "evt-uid", 0, expected_holder=USER)
    assert inter.followup.send.call_args.args[0] == texts.CANCELLED
    assert queued == [("slot", "evt-uid:0", "upsert")]
    assert notion.calls == []
    assert not lock.locked()


async def test_cancel_of_slot_no_longer_held_queues_nothing(queued) -> None:
    """The slot was freed meanwhile (admin, hand edit): nothing changed, nothing queued."""
    from bot.ui.views import CancelConfirmView

    ctx, notion = _ctx_with_hanging_notion()
    event = _event()
    lock = ctx.get_user_lock(USER)
    await lock.acquire()
    view = CancelConfirmView(
        ctx=ctx, event=event, slot_index=0, slot_unix=1751400000, lock=lock, user_id=USER
    )
    inter = _inter()

    with patch("bot.db.bookings.release_slot_lock", new=AsyncMock(return_value=False)):
        await view._handle_confirm(inter)
    await _settle()

    assert inter.followup.send.call_args.args[0] == texts.CANCELLED
    assert queued == []
    assert notion.calls == []
