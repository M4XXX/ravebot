"""Tests for bot/services/event_replenish.py."""
import contextlib
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.notion.models import EventInfo
from bot.services.event_replenish import replenish_events
from tests.notion.factories import make_event

TODAY = datetime.date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _fixed_today():
    with patch("bot.services.event_replenish.local_today", return_value=TODAY):
        yield


async def _rep(cog, events) -> int:
    return await replenish_events(cog.ctx.db, cog.ctx.notion_client, 99999, events)


def _make_cog_with_bot() -> SimpleNamespace:
    """Stand-in exposing ctx.db and ctx.notion_client."""
    ctx = SimpleNamespace(db=MagicMock(), notion_client=MagicMock())
    return SimpleNamespace(ctx=ctx)


# ---------------------------------------------------------------------------
# Story 7.1: replenish_events
# ---------------------------------------------------------------------------


def _make_event_with_date(event_id: str, date_str: str) -> EventInfo:
    """Build a typed event on the given ISO date."""
    return make_event(
        event_id=event_id, page_id=event_id, date=datetime.date.fromisoformat(date_str)
    )


def _new_event(page_id: str = "new-page") -> EventInfo:
    return make_event(page_id=page_id, event_id="x")


def _wire_client(cog, create=None, exists=None) -> None:
    client = cog.ctx.notion_client
    client.create_event = create or AsyncMock(return_value=_new_event())
    client.event_id_exists = exists or AsyncMock(return_value=False)


@contextlib.contextmanager
def _cfg(
    lookahead: str | None,
    interval: str | None,
    slots: str | None,
    start: str | None = None,
    minutes: str | None = None,
):
    """Config values by key, for the cog and for the event-defaults helper."""
    values = {
        "event_lookahead_count": lookahead,
        "event_interval_days": interval,
        "slots_per_event": slots,
        "default_start_time": start,
        "slot_duration_minutes": minutes,
    }

    async def fake_get_value(pool, guild_id, key):
        return values.get(key)

    getter = AsyncMock(side_effect=fake_get_value)
    with patch("bot.services.event_replenish.get_value", new=getter), \
         patch("bot.services.event_defaults.get_value", new=getter):
        yield


@pytest.mark.asyncio
async def test_replenish_events_creates_correct_count() -> None:
    cog = _make_cog_with_bot()
    events = [
        _make_event_with_date("page-a", "2026-06-07"),
        _make_event_with_date("page-b", "2026-06-14"),
        _make_event_with_date("page-c", "2026-06-21"),
    ]
    _wire_client(cog)
    with _cfg("6", "7", "6"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        await _rep(cog, events)
    assert cog.ctx.notion_client.create_event.await_count == 3


@pytest.mark.asyncio
async def test_replenish_events_skips_when_at_lookahead() -> None:
    cog = _make_cog_with_bot()
    events = [_make_event_with_date(f"page-{i}", f"2026-06-{i+1:02d}") for i in range(6)]
    _wire_client(cog)
    with patch("bot.services.event_replenish.get_value", new=AsyncMock(return_value="6")):
        await _rep(cog, events)
    cog.ctx.notion_client.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_replenish_events_id_format_dates_and_configured_shape() -> None:
    """New events take Start/Slot length/slot count from config, not from the latest event."""
    cog = _make_cog_with_bot()
    events = [
        make_event(page_id="a", date=datetime.date(2026, 6, 7), start=datetime.time(18, 0),
                   slot_minutes=30),
        make_event(page_id="b", date=datetime.date(2026, 6, 14), start=datetime.time(21, 30),
                   slot_minutes=45),
    ]
    _wire_client(cog)
    with _cfg("4", "7", "5", start="19:15", minutes="50"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        await _rep(cog, events)

    calls = cog.ctx.notion_client.create_event.await_args_list
    assert [c.args for c in calls] == [
        ("2026-06-21 Rave Night", datetime.date(2026, 6, 21), datetime.time(19, 15), 5, 50),
        ("2026-06-28 Rave Night", datetime.date(2026, 6, 28), datetime.time(19, 15), 5, 50),
    ]


@pytest.mark.asyncio
async def test_replenish_events_ignores_latest_event_shape_when_config_unset() -> None:
    cog = _make_cog_with_bot()
    events = [
        make_event(page_id="b", date=datetime.date(2026, 6, 14), start=datetime.time(21, 30),
                   slot_minutes=45),
    ]
    _wire_client(cog)
    with _cfg("2", "7", None), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        await _rep(cog, events)
    args = cog.ctx.notion_client.create_event.await_args.args
    assert args[2:] == (datetime.time(20, 0), 6, 60)


@pytest.mark.asyncio
async def test_replenish_events_defaults_without_existing_events() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("1", "7", "6"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        await _rep(cog, [])
    args = cog.ctx.notion_client.create_event.await_args.args
    assert args[2] == datetime.time(20, 0)
    assert args[3:] == (6, 60)
    assert args[0].endswith(" Rave Night")


@pytest.mark.asyncio
async def test_replenish_events_inits_slot_locks_with_page_id() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog, create=AsyncMock(return_value=_new_event("created-page")))
    with _cfg("2", "7", "4"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()) as init:
        await _rep(cog, [_make_event_with_date("a", "2026-06-07")])
    init.assert_awaited_once_with(cog.ctx.db, "created-page", 4)


@pytest.mark.asyncio
async def test_replenish_events_skips_existing_id() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog, exists=AsyncMock(side_effect=[True, False]))
    with _cfg("3", "7", "6"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()) as init:
        await _rep(cog, [_make_event_with_date("a", "2026-06-07")])
    assert cog.ctx.notion_client.event_id_exists.await_count == 2
    assert cog.ctx.notion_client.create_event.await_count == 1
    assert cog.ctx.notion_client.create_event.await_args.args[0] == "2026-06-21 Rave Night"
    assert init.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["NotionWriteError", "DuplicateEventError"])
async def test_replenish_events_one_failure_does_not_stop_next(error_name: str) -> None:
    from bot.notion import client as nc

    error = nc.NotionWriteError("write failed") if error_name == "NotionWriteError" \
        else nc.DuplicateEventError("dup")
    cog = _make_cog_with_bot()
    create = AsyncMock(side_effect=[error, _new_event()])
    _wire_client(cog, create=create)
    with _cfg("3", "7", "6"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        await _rep(cog, [_make_event_with_date("a", "2026-06-07")])
    assert create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "0", "-3"])
async def test_replenish_events_invalid_slots_config_uses_default(bad: str) -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("1", "7", bad), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        await _rep(cog, [])
    assert cog.ctx.notion_client.create_event.await_args.args[3] == 6


@pytest.mark.asyncio
async def test_replenish_events_value_error_does_not_abort_loop() -> None:
    cog = _make_cog_with_bot()
    create = AsyncMock(side_effect=[ValueError("bad"), _new_event()])
    _wire_client(cog, create=create)
    with _cfg("3", "7", "6"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        await _rep(cog, [_make_event_with_date("a", "2026-06-07")])
    assert create.await_count == 2


# ---------------------------------------------------------------------------
# Story 7.3: replenish_events uses slots_per_event from config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replenish_events_uses_slots_per_event_from_config() -> None:
    """replenish_events calls init_event_slots with slots_per_event from config."""
    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # 1 event, need 2 more

    _wire_client(cog, create=AsyncMock(return_value=_new_event("new-page-id")))

    init_calls: list[tuple[object, str, int]] = []

    async def mock_init_event_slots(pool: object, event_id: str, slots_per_event: int) -> None:
        init_calls.append((pool, event_id, slots_per_event))

    with _cfg("3", "7", "3"), \
         patch("bot.services.event_replenish.init_event_slots", new=mock_init_event_slots):
        await _rep(cog, events)

    # Two events created → init_event_slots called twice with slots=3
    assert len(init_calls) == 2
    assert init_calls[0][1] == "new-page-id"
    assert init_calls[0][2] == 3
    assert init_calls[1][1] == "new-page-id"
    assert init_calls[1][2] == 3


@pytest.mark.asyncio
async def test_replenish_events_defaults_to_6_slots_when_unset() -> None:
    """replenish_events uses slots_per_event=6 when config key is not set."""
    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # 1 event, need 2 more

    _wire_client(cog, create=AsyncMock(return_value=_new_event("new-page-id")))

    init_calls: list[tuple[object, str, int]] = []

    async def mock_init_event_slots(pool: object, event_id: str, slots_per_event: int) -> None:
        init_calls.append((pool, event_id, slots_per_event))

    with _cfg("3", "7", None), \
         patch("bot.services.event_replenish.init_event_slots", new=mock_init_event_slots):
        await _rep(cog, events)

    assert len(init_calls) == 2
    assert init_calls[0][2] == 6   # default
    assert init_calls[1][2] == 6




# ---------------------------------------------------------------------------
# Replacing done, canceled and past events
# ---------------------------------------------------------------------------


def _ev(date_str: str, status: str | None = "Planned") -> EventInfo:
    return make_event(
        event_id=date_str, page_id=date_str, status=status,
        date=datetime.date.fromisoformat(date_str),
    )


async def _run(events, lookahead="3", interval="7", exists=None):
    cog = _make_cog_with_bot()
    _wire_client(cog, exists=exists)
    with _cfg(lookahead, interval, "6"), \
         patch("bot.services.event_replenish.init_event_slots", new=AsyncMock()):
        n = await replenish_events(cog.ctx.db, cog.ctx.notion_client, 1, events)
    ids = [c.args[0] for c in cog.ctx.notion_client.create_event.await_args_list]
    return n, ids


@pytest.mark.asyncio
async def test_done_event_is_replaced() -> None:
    events = [_ev("2026-06-07", "Done"), _ev("2026-06-14"), _ev("2026-06-21")]
    n, ids = await _run(events)
    assert n == 1
    assert ids == ["2026-06-28 Rave Night"]


@pytest.mark.asyncio
async def test_canceled_event_is_replaced() -> None:
    events = [_ev("2026-06-07"), _ev("2026-06-14", "Canceled"), _ev("2026-06-21")]
    n, ids = await _run(events)
    assert n == 1
    assert ids == ["2026-06-28 Rave Night"]


@pytest.mark.asyncio
async def test_done_and_canceled_each_create_one() -> None:
    events = [_ev("2026-06-07", "Done"), _ev("2026-06-14", "Canceled"), _ev("2026-06-21")]
    n, ids = await _run(events)
    assert n == 2
    assert ids == ["2026-06-28 Rave Night", "2026-07-05 Rave Night"]


@pytest.mark.asyncio
async def test_second_poll_with_new_events_creates_nothing() -> None:
    events = [_ev("2026-06-07", "Done"), _ev("2026-06-14"), _ev("2026-06-21"),
              _ev("2026-06-28")]
    n, ids = await _run(events)
    assert (n, ids) == (0, [])


@pytest.mark.asyncio
async def test_existing_id_is_skipped_on_repeat() -> None:
    events = [_ev("2026-06-07", "Done"), _ev("2026-06-14"), _ev("2026-06-21")]
    n, ids = await _run(events, exists=AsyncMock(return_value=True))
    assert (n, ids) == (0, [])


@pytest.mark.asyncio
async def test_all_events_in_the_past_create_lookahead_from_today() -> None:
    events = [_ev("2026-05-01"), _ev("2026-05-08")]
    n, ids = await _run(events)
    assert ids == ["2026-06-08 Rave Night", "2026-06-15 Rave Night", "2026-06-22 Rave Night"]
    assert all(i[:10] > TODAY.isoformat() for i in ids)


@pytest.mark.asyncio
async def test_no_events_create_lookahead_from_today() -> None:
    n, ids = await _run([])
    assert n == 3
    assert ids == ["2026-06-08 Rave Night", "2026-06-15 Rave Night", "2026-06-22 Rave Night"]


@pytest.mark.asyncio
async def test_base_date_includes_canceled_latest_event() -> None:
    events = [_ev("2026-06-07"), _ev("2026-06-14"), _ev("2026-06-21", "Canceled")]
    n, ids = await _run(events)
    assert ids == ["2026-06-28 Rave Night"]


@pytest.mark.asyncio
async def test_lookahead_met_creates_nothing() -> None:
    events = [_ev("2026-06-07"), _ev("2026-06-14"), _ev("2026-06-21")]
    assert await _run(events) == (0, [])


@pytest.mark.asyncio
async def test_event_today_counts_as_active() -> None:
    events = [_ev("2026-06-01"), _ev("2026-06-07"), _ev("2026-06-14")]
    assert await _run(events) == (0, [])


@pytest.mark.asyncio
async def test_none_client_or_db_creates_nothing() -> None:
    assert await replenish_events(None, MagicMock(), 1, []) == 0
    assert await replenish_events(MagicMock(), None, 1, []) == 0


@pytest.mark.asyncio
async def test_concurrent_replenish_never_creates_the_same_event_twice() -> None:
    import asyncio

    created_ids: list[str] = []
    seen: set[str] = set()

    async def exists(event_id: str) -> bool:
        return event_id in seen

    async def create(event_id, *args, **kwargs):
        await asyncio.sleep(0)
        seen.add(event_id)
        created_ids.append(event_id)
        return _new_event()

    client = MagicMock()
    client.event_id_exists = exists
    client.create_event = create
    db = MagicMock()
    with patch("bot.services.event_replenish.get_value", AsyncMock(return_value=None)), patch(
        "bot.services.event_replenish.default_event_shape",
        AsyncMock(return_value=SimpleNamespace(start=None, slot_count=6, slot_minutes=60)),
    ), patch("bot.services.event_replenish.init_event_slots", AsyncMock()):
        await asyncio.gather(
            replenish_events(db, client, 1, []), replenish_events(db, client, 1, [])
        )
    assert len(created_ids) == len(set(created_ids)) == 6


@pytest.mark.asyncio
async def test_replenish_without_events_loads_them_inside_the_lock() -> None:
    """A poll that created the events first leaves the second caller nothing to do."""
    import asyncio

    created: list[datetime.date] = []

    async def exists(event_id: str) -> bool:
        return any(event_id.startswith(d.isoformat()) for d in created)

    async def create(event_id, new_date, *a, **k):
        await asyncio.sleep(0)
        created.append(new_date)
        return _new_event()

    async def list_events(**kwargs):
        return [make_event(event_id=str(d), page_id=str(d), date=d) for d in created]

    client = MagicMock()
    client.event_id_exists = exists
    client.create_event = create
    client.list_events = list_events
    shape = SimpleNamespace(start=None, slot_count=6, slot_minutes=60)
    with patch("bot.services.event_replenish.get_value", AsyncMock(return_value=None)), patch(
        "bot.services.event_replenish.default_event_shape", AsyncMock(return_value=shape)
    ), patch("bot.services.event_replenish.init_event_slots", AsyncMock()):
        poll = replenish_events(MagicMock(), client, 1, [])  # stale list from a poll
        wizard = replenish_events(MagicMock(), client, 1)  # loads inside the lock
        results = await asyncio.gather(poll, wizard)
    assert results == [6, 0]
    assert len(created) == 6
