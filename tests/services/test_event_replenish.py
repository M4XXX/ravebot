"""Tests for bot/services/event_replenish.py."""
import contextlib
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from bot.db.events import DuplicateEventError, EventDraft
from bot.notion.models import EventInfo
from bot.services.event_replenish import replenish_events
from tests.fake_events import fake_events
from tests.notion.factories import make_event

TODAY = datetime.date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _fixed_today():
    with patch("bot.services.event_replenish.local_today", return_value=TODAY):
        yield


@pytest.fixture(autouse=True)
def _no_rules():
    """No schedule rules or defaults: new events get no genre or location."""
    async def load_resolver(db, guild_id, interval_days):
        return lambda field, day: None

    with patch("bot.services.schedule.load_resolver", new=load_resolver):
        yield


@pytest.fixture(autouse=True)
def _manual():
    """Manual page ids as the repository returns them (none by default)."""
    with patch(
        "bot.services.event_replenish.get_manual_page_ids", new=AsyncMock(return_value=set())
    ) as mocked:
        yield mocked


# The events repository as replenish sees it: create_event(event_id, date, start,
# slot_count, slot_minutes) records each insert_event call; event_id_exists(event_id).
REPO = SimpleNamespace()


@pytest.fixture(autouse=True)
def _repo():
    """insert_event / event_id_exists / changed patched; REPO holds the mocks."""
    REPO.create_event = AsyncMock(side_effect=lambda *a: _new_event())
    REPO.event_id_exists = AsyncMock(return_value=False)
    REPO.drafts = []
    REPO.uids = []

    async def insert_event(db, draft, uid=None):
        assert isinstance(draft, EventDraft)
        REPO.drafts.append(draft)
        event = await REPO.create_event(
            draft.event_id, draft.date, draft.start, draft.slot_count, draft.slot_minutes
        )
        REPO.uids.append(event.page_id)
        return event

    async def event_id_exists(db, event_id):
        return await REPO.event_id_exists(event_id)

    with patch("bot.services.event_replenish.db_events.insert_event", new=insert_event), \
         patch("bot.services.event_replenish.db_events.event_id_exists", new=event_id_exists), \
         patch("bot.services.event_replenish.changed") as changed:
        REPO.changed = changed
        yield REPO


async def _rep(cog, events) -> int:
    return await replenish_events(cog.ctx, 99999, events)


def _ctx(**kw) -> SimpleNamespace:
    """Stand-in BotContext: db, a Notion client that must stay untouched, no maintenance."""
    values = {"db": MagicMock(), "notion_client": MagicMock(), "mirror": None,
              "maintenance": None}
    values.update(kw)
    return SimpleNamespace(**values)


def _make_cog_with_bot() -> SimpleNamespace:
    return SimpleNamespace(ctx=_ctx())


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
    if create is not None:
        REPO.create_event = create
    if exists is not None:
        REPO.event_id_exists = exists


@contextlib.contextmanager
def _cfg(
    lookahead: str | None,
    interval: str | None,
    slots: str | None,
    start: str | None = None,
    minutes: str | None = None,
    schedule: str | None = None,
    first: str | None = None,
):
    """Config values by key, for the cog and for the event-defaults helper.

    Yields the mock that replaces `set_value` in the replenish module.
    """
    values = {
        "event_lookahead_count": lookahead,
        "event_interval_days": interval,
        "slots_per_event": slots,
        "default_start_time": start,
        "slot_duration_minutes": minutes,
        "event_schedule_enabled": schedule,
        "first_event_date": first,
    }

    async def fake_get_value(pool, guild_id, key):
        return values.get(key)

    getter = AsyncMock(side_effect=fake_get_value)
    with patch("bot.services.event_replenish.get_value", new=getter), \
         patch("bot.services.event_defaults.get_value", new=getter), \
         patch("bot.services.event_replenish.set_value", new=AsyncMock()) as setter:
        yield setter


@pytest.mark.asyncio
async def test_replenish_events_creates_correct_count() -> None:
    cog = _make_cog_with_bot()
    events = [
        _make_event_with_date("page-a", "2026-06-07"),
        _make_event_with_date("page-b", "2026-06-14"),
        _make_event_with_date("page-c", "2026-06-21"),
    ]
    _wire_client(cog)
    with _cfg("6", "7", "6"):
        await _rep(cog, events)
    assert REPO.create_event.await_count == 3


@pytest.mark.asyncio
async def test_replenish_events_skips_when_at_lookahead() -> None:
    cog = _make_cog_with_bot()
    events = [_make_event_with_date(f"page-{i}", f"2026-06-{i+1:02d}") for i in range(6)]
    _wire_client(cog)
    with patch("bot.services.event_replenish.get_value", new=AsyncMock(return_value="6")):
        await _rep(cog, events)
    REPO.create_event.assert_not_called()


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
    with _cfg("4", "7", "5", start="19:15", minutes="50"):
        await _rep(cog, events)

    calls = REPO.create_event.await_args_list
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
    with _cfg("2", "7", None):
        await _rep(cog, events)
    args = REPO.create_event.await_args.args
    assert args[2:] == (datetime.time(20, 0), 6, 60)


@pytest.mark.asyncio
async def test_replenish_events_defaults_without_existing_events() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("1", "7", "6"):
        await _rep(cog, [])
    args = REPO.create_event.await_args.args
    assert args[2] == datetime.time(20, 0)
    assert args[3:] == (6, 60)
    assert args[0].endswith(" Rave Night")


@pytest.mark.asyncio
async def test_replenish_writes_postgres_and_queues_event_and_slots() -> None:
    cog = _make_cog_with_bot()
    created = make_event(page_id="created-page", event_id="x", slot_count=3)
    _wire_client(cog, create=AsyncMock(return_value=created))
    with _cfg("2", "7", "3", start="19:15", minutes="50"):
        assert await _rep(cog, [_make_event_with_date("a", "2026-06-07")]) == 1
    assert REPO.drafts == [
        EventDraft(
            event_id="2026-06-14 Rave Night", date=datetime.date(2026, 6, 14),
            start=datetime.time(19, 15), slot_minutes=50, slot_count=3,
        )
    ]
    assert REPO.changed.call_args_list == [
        call(cog.ctx, "event", "created-page"),
        call(cog.ctx, "slot", "created-page:0"),
        call(cog.ctx, "slot", "created-page:1"),
        call(cog.ctx, "slot", "created-page:2"),
    ]
    assert cog.ctx.notion_client.method_calls == []


@pytest.mark.asyncio
async def test_replenish_events_skips_existing_id() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog, exists=AsyncMock(side_effect=[True, False]))
    with _cfg("3", "7", "6"):
        await _rep(cog, [_make_event_with_date("a", "2026-06-07")])
    assert REPO.event_id_exists.await_count == 2
    assert REPO.create_event.await_count == 1
    assert REPO.create_event.await_args.args[0] == "2026-06-21 Rave Night"
    assert [c.args[1] for c in REPO.changed.call_args_list] == ["event"] + ["slot"] * 6


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["DuplicateEventError", "RuntimeError"])
async def test_replenish_events_one_failure_does_not_stop_next(error_name: str) -> None:
    error = DuplicateEventError("dup") if error_name == "DuplicateEventError" \
        else RuntimeError("db down")
    cog = _make_cog_with_bot()
    create = AsyncMock(side_effect=[error, _new_event()])
    _wire_client(cog, create=create)
    with _cfg("3", "7", "6"):
        assert await _rep(cog, [_make_event_with_date("a", "2026-06-07")]) == 1
    assert create.await_count == 2
    # only the created event is queued for the mirror
    assert [c.args[1] for c in REPO.changed.call_args_list] == ["event"] + ["slot"] * 6


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "0", "-3"])
async def test_replenish_events_invalid_slots_config_uses_default(bad: str) -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("1", "7", bad):
        await _rep(cog, [])
    assert REPO.create_event.await_args.args[3] == 6


@pytest.mark.asyncio
async def test_replenish_events_value_error_does_not_abort_loop() -> None:
    cog = _make_cog_with_bot()
    create = AsyncMock(side_effect=[ValueError("bad"), _new_event()])
    _wire_client(cog, create=create)
    with _cfg("3", "7", "6"):
        await _rep(cog, [_make_event_with_date("a", "2026-06-07")])
    assert create.await_count == 2


# ---------------------------------------------------------------------------
# Story 7.3: replenish_events uses slots_per_event from config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replenish_events_uses_slots_per_event_from_config() -> None:
    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # 1 event, need 2 more
    with _cfg("3", "7", "3"):
        await _rep(cog, events)
    assert [d.slot_count for d in REPO.drafts] == [3, 3]


@pytest.mark.asyncio
async def test_replenish_events_defaults_to_6_slots_when_unset() -> None:
    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # 1 event, need 2 more
    with _cfg("3", "7", None):
        await _rep(cog, events)
    assert [d.slot_count for d in REPO.drafts] == [6, 6]




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
    with _cfg(lookahead, interval, "6"):
        n = await replenish_events(cog.ctx, 1, events)
    ids = [c.args[0] for c in REPO.create_event.await_args_list]
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
async def test_no_db_creates_nothing() -> None:
    assert await replenish_events(_ctx(db=None), 1, []) == 0
    REPO.create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_works_without_notion() -> None:
    cog = SimpleNamespace(ctx=_ctx(notion_client=None))
    with _cfg("1", "7", "6"):
        assert await _rep(cog, []) == 1


@pytest.mark.asyncio
async def test_maintenance_creates_nothing() -> None:
    """An import replaces the events: replenish waits until it is done."""
    cog = SimpleNamespace(ctx=_ctx(maintenance="restoring"))
    with _cfg("3", "7", "6"):
        assert await _rep(cog, []) == 0
    REPO.create_event.assert_not_awaited()
    REPO.event_id_exists.assert_not_awaited()
    REPO.changed.assert_not_called()


@pytest.mark.asyncio
async def test_maintenance_set_while_waiting_for_the_lock_creates_nothing() -> None:
    """A rebuild that starts while replenish waits for the lock wins: nothing is created."""
    import asyncio

    from bot.services.event_replenish import replenish_guard

    ctx = _ctx()
    with _cfg("3", "7", "6"):
        async with replenish_guard():
            task = asyncio.create_task(_rep(SimpleNamespace(ctx=ctx), []))
            await asyncio.sleep(0)
            ctx.maintenance = "restoring"
        assert await task == 0
    REPO.create_event.assert_not_awaited()
    REPO.changed.assert_not_called()


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

    REPO.event_id_exists = exists
    REPO.create_event = create
    ctx = _ctx()
    with patch("bot.services.event_replenish.get_value", AsyncMock(return_value=None)), patch(
        "bot.services.event_replenish.default_event_shape",
        AsyncMock(return_value=SimpleNamespace(start=None, slot_count=6, slot_minutes=60)),
    ):
        await asyncio.gather(replenish_events(ctx, 1, []), replenish_events(ctx, 1, []))
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

    async def list_events(db):
        return [make_event(event_id=str(d), page_id=str(d), date=d) for d in created]

    REPO.event_id_exists = exists
    REPO.create_event = create
    fake_events.list_events = AsyncMock(side_effect=list_events)
    shape = SimpleNamespace(start=None, slot_count=6, slot_minutes=60)
    with patch("bot.services.event_replenish.get_value", AsyncMock(return_value=None)), patch(
        "bot.services.event_replenish.default_event_shape", AsyncMock(return_value=shape)
    ):
        poll = replenish_events(_ctx(), 1, [])  # stale list from a poll
        wizard = replenish_events(_ctx(), 1)  # loads inside the lock
        results = await asyncio.gather(poll, wizard)
    assert results == [6, 0]
    assert len(created) == 6
    fake_events.list_events.assert_awaited_once()


# ---------------------------------------------------------------------------
# Event scheduling: schedule switch, first event date, manual events
# ---------------------------------------------------------------------------


def _created_dates(cog) -> list[datetime.date]:
    return [c.args[1] for c in REPO.create_event.await_args_list]


@pytest.mark.asyncio
async def test_schedule_off_creates_nothing() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("3", "14", "6", schedule="false"):
        assert await _rep(cog, []) == 0
    REPO.create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_date_used_for_first_event_then_interval() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("3", "14", "6", first="2026-06-06"):
        await _rep(cog, [])
    assert _created_dates(cog) == [
        datetime.date(2026, 6, 6), datetime.date(2026, 6, 20), datetime.date(2026, 7, 4)
    ]


@pytest.mark.asyncio
async def test_first_date_cleared_after_creation() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("1", "14", "6", first="2026-06-06") as setter:
        await _rep(cog, [])
    setter.assert_awaited_once_with(cog.ctx.db, 99999, "first_event_date", "")


@pytest.mark.asyncio
async def test_first_date_ignored_when_scheduled_events_ahead() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("3", "14", "6", first="2026-06-06") as setter:
        await _rep(cog, [_make_event_with_date("a", "2026-06-10")])
    assert _created_dates(cog) == [datetime.date(2026, 6, 24), datetime.date(2026, 7, 8)]
    setter.assert_awaited_once_with(cog.ctx.db, 99999, "first_event_date", "")


@pytest.mark.asyncio
async def test_past_first_date_ignored_and_cleared() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("2", "14", "6", first="2026-05-20") as setter:
        await _rep(cog, [])
    assert _created_dates(cog) == [datetime.date(2026, 6, 15), datetime.date(2026, 6, 29)]
    setter.assert_awaited_once_with(cog.ctx.db, 99999, "first_event_date", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [None, "", "garbage"])
async def test_empty_or_invalid_first_date_keeps_todays_rule(first) -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("2", "14", "6", first=first) as setter:
        await _rep(cog, [])
    assert _created_dates(cog) == [datetime.date(2026, 6, 15), datetime.date(2026, 6, 29)]
    if first:
        setter.assert_awaited_once()
    else:
        setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_events_not_counted_and_do_not_shift_base(_manual) -> None:
    _manual.return_value = {"m1", "m2"}
    cog = _make_cog_with_bot()
    _wire_client(cog)
    events = [
        _make_event_with_date("m1", "2026-06-03"),
        _make_event_with_date("m2", "2026-06-30"),
    ]
    with _cfg("3", "14", "6"):
        created = await _rep(cog, events)
    assert created == 3
    assert _created_dates(cog) == [
        datetime.date(2026, 6, 15), datetime.date(2026, 6, 29), datetime.date(2026, 7, 13)
    ]
    _manual.assert_awaited_once_with(cog.ctx.db, ["m1", "m2"])


@pytest.mark.asyncio
async def test_manual_event_on_scheduled_date_is_skipped_without_duplicate(_manual) -> None:
    _manual.return_value = {"m1"}
    cog = _make_cog_with_bot()

    async def exists(event_id: str) -> bool:
        return event_id == "2026-06-15 Rave Night"

    _wire_client(cog, exists=AsyncMock(side_effect=exists))
    with _cfg("3", "14", "6"):
        created = await _rep(cog, [_make_event_with_date("m1", "2026-06-15")])
    assert created == 3
    assert datetime.date(2026, 6, 15) not in _created_dates(cog)
    assert _created_dates(cog) == [
        datetime.date(2026, 6, 29), datetime.date(2026, 7, 13), datetime.date(2026, 7, 27)
    ]


@pytest.mark.asyncio
async def test_write_failures_stop_after_missing_attempts() -> None:
    from bot.notion import client as nc

    cog = _make_cog_with_bot()
    create = AsyncMock(side_effect=nc.NotionWriteError("write failed"))
    _wire_client(cog, create=create)
    with _cfg("4", "7", "6"):
        assert await _rep(cog, []) == 0
    assert create.await_count == 4


@pytest.mark.asyncio
async def test_many_manual_dates_stop_at_candidate_cap(_manual) -> None:
    start = datetime.date(2026, 6, 8)
    events = [
        make_event(page_id=f"m{k}", event_id=f"m{k}", date=start + datetime.timedelta(days=7 * k))
        for k in range(60)
    ]
    _manual.return_value = {e.page_id for e in events}
    cog = _make_cog_with_bot()
    _wire_client(cog, exists=AsyncMock(return_value=True))
    with _cfg("2", "7", "6"):
        assert await _rep(cog, events) == 0
    assert REPO.event_id_exists.await_count == 2 + 52
    REPO.create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_events_get_resolved_genre_and_location() -> None:
    """Each draft carries the rule/default value for its date, markers false."""
    cog = _make_cog_with_bot()
    calls = []

    async def load_resolver(db, guild_id, interval_days):
        calls.append(interval_days)
        return lambda field, day: f"{field}-{day.day}"

    with _cfg("2", "7", "6"), patch(
        "bot.services.schedule.load_resolver", new=load_resolver
    ):
        assert await _rep(cog, []) == 2
    assert calls == [7]  # rules read once per run
    assert [(d.genre, d.location) for d in REPO.drafts] == [
        ("genre-8", "location-8"), ("genre-15", "location-15")
    ]
    assert not any(d.genre_by_hand or d.location_by_hand for d in REPO.drafts)
