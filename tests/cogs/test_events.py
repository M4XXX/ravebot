"""Tests for bot/cogs/events.py — DB resilience guard pattern and Story 3.1 poll logic."""
import contextlib
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.notion.models import EventInfo
from bot.ui.embeds import ErrorCode
from tests.notion.factories import make_event, make_slot


def make_ctx(db: object = None, log: object = None) -> MagicMock:
    ctx = MagicMock()
    ctx.db = db
    ctx.log_service = log
    return ctx


@pytest.mark.asyncio
async def test_guard_db_returns_false_and_posts_error_when_db_none() -> None:
    """_guard_db returns False and posts ERR_DB_UNAVAILABLE when db pool is None."""
    from bot.cogs.events import EventsCog

    log = MagicMock()
    log.post_error = AsyncMock()
    ctx = make_ctx(db=None, log=log)
    cog = EventsCog(ctx)

    result = await cog._guard_db("test_op")

    assert result is False
    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_DB_UNAVAILABLE


@pytest.mark.asyncio
async def test_guard_db_returns_false_and_posts_error_on_connection_error() -> None:
    """_guard_db returns False and posts ERR_DB_UNAVAILABLE on asyncpg connection failure."""
    from bot.cogs.events import EventsCog

    log = MagicMock()
    log.post_error = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.fetchval = AsyncMock(side_effect=asyncpg.PostgresConnectionError("lost"))
    ctx = make_ctx(db=mock_pool, log=log)
    cog = EventsCog(ctx)

    result = await cog._guard_db("test_op")

    assert result is False
    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_DB_UNAVAILABLE


@pytest.mark.asyncio
async def test_guard_db_returns_true_when_db_healthy() -> None:
    """_guard_db returns True when DB responds to SELECT 1 without error."""
    from bot.cogs.events import EventsCog

    mock_pool = MagicMock()
    mock_pool.fetchval = AsyncMock(return_value=1)
    ctx = make_ctx(db=mock_pool, log=None)
    cog = EventsCog(ctx)

    result = await cog._guard_db("test_op")

    assert result is True


@pytest.mark.asyncio
async def test_guard_db_no_log_service_does_not_crash_when_db_none() -> None:
    """_guard_db handles None log_service gracefully when DB pool is also None."""
    from bot.cogs.events import EventsCog

    ctx = make_ctx(db=None, log=None)
    cog = EventsCog(ctx)

    result = await cog._guard_db("test_op")

    assert result is False


# ---------------------------------------------------------------------------
# Story 3.1: _build_snapshot, _get_guild_id, _poll_notion
# ---------------------------------------------------------------------------

def _make_pages(*ids: str) -> list[EventInfo]:
    return [
        make_event(event_id=pid, page_id=pid, date=datetime.date(2026, 6, i + 1))
        for i, pid in enumerate(ids)
    ]


def _make_cog_with_bot(guilds: list = None) -> "EventsCog":
    """Create EventsCog with a mock bot attribute for testing guild access."""
    from bot.cogs.events import EventsCog

    mock_db = MagicMock()
    mock_db.fetchval = AsyncMock(return_value=1)
    log = MagicMock()
    log.post_error = AsyncMock()
    notion = MagicMock()
    notion.list_events = AsyncMock()
    ctx = MagicMock()
    ctx.db = mock_db
    ctx.log_service = log
    ctx.notion_client = notion
    sync = MagicMock()
    sync.run_cycle = AsyncMock(return_value=[])
    ctx.notion_sync_service = sync

    cog = EventsCog(ctx)
    mock_bot = MagicMock()
    mock_bot.guilds = guilds if guilds is not None else [MagicMock(id=99999)]
    # disnake sets .bot as a plain attribute via _inject; set directly on the instance
    cog.bot = mock_bot
    return cog


def test_build_snapshot_stable() -> None:
    """_build_snapshot produces identical output regardless of input event order."""
    from bot.cogs.events import EventsCog

    events_a = _make_pages("page-z", "page-a")
    events_b = list(reversed(events_a))

    snap_a = EventsCog._build_snapshot(events_a)
    snap_b = EventsCog._build_snapshot(events_b)

    assert snap_a == snap_b
    parsed = json.loads(snap_a)
    assert [e["page_id"] for e in parsed] == ["page-a", "page-z"]
    entry = parsed[0]
    assert entry["event_id"] == "page-a"
    assert entry["date"] == "2026-06-02"
    assert entry["start"] == "20:00"
    assert entry["slot_count"] == 6
    assert entry["slot_minutes"] == 60
    assert entry["status"] == "Planned"


def test_build_snapshot_changes_when_assignment_changes() -> None:
    """Changing a slot's DJ changes the snapshot."""
    from bot.cogs.events import EventsCog

    before = [make_event(slots=[make_slot(1, dj_page_id="dj-a"), make_slot(2)])]
    after = [make_event(slots=[make_slot(1, dj_page_id="dj-a"), make_slot(2, dj_page_id="dj-b")])]
    swapped = [make_event(slots=[make_slot(1, dj_page_id="dj-b"), make_slot(2)])]

    snap = EventsCog._build_snapshot(before)
    assert snap != EventsCog._build_snapshot(after)
    assert snap != EventsCog._build_snapshot(swapped)


def test_build_snapshot_slot_order_independent() -> None:
    """Slot pairs are sorted by slot number."""
    from bot.cogs.events import EventsCog

    a = [make_event(slots=[make_slot(1, dj_page_id="x"), make_slot(2, dj_page_id="y")])]
    b = [make_event(slots=[make_slot(2, dj_page_id="y"), make_slot(1, dj_page_id="x")])]
    assert EventsCog._build_snapshot(a) == EventsCog._build_snapshot(b)


def test_build_snapshot_empty() -> None:
    """_build_snapshot returns '[]' for an empty event list."""
    from bot.cogs.events import EventsCog

    assert EventsCog._build_snapshot([]) == "[]"


def test_get_guild_id_returns_first_guild_id() -> None:
    """_get_guild_id returns the id of the first guild when guilds are present."""
    cog = _make_cog_with_bot(guilds=[MagicMock(id=12345)])

    result = cog._get_guild_id()

    assert result == 12345


def test_get_guild_id_no_guilds() -> None:
    """_get_guild_id returns None and logs a warning when bot has no guilds."""
    cog = _make_cog_with_bot(guilds=[])

    result = cog._get_guild_id()

    assert result is None


@pytest.mark.asyncio
async def test_poll_notion_no_change_no_trigger() -> None:
    """_poll_notion: when snapshot is unchanged, _trigger_lineup_update is NOT called."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a", "page-b")
    snapshot = EventsCog._build_snapshot(events)

    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=snapshot)), \
         patch("bot.cogs.events.set_value", new=AsyncMock()) as mock_set, \
         patch.object(cog, "_auto_create_events", new=AsyncMock()), \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()) as mock_trigger:
        await EventsCog._poll_notion.coro(cog)

    mock_trigger.assert_not_called()
    mock_set.assert_not_called()


@pytest.mark.asyncio
async def test_poll_notion_change_triggers_update() -> None:
    """_poll_notion: when snapshot changes, _trigger_lineup_update is called and snapshot is saved."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a", "page-b")
    old_snapshot = EventsCog._build_snapshot(_make_pages("page-a"))  # only one event before

    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=old_snapshot)), \
         patch("bot.cogs.events.set_value", new=AsyncMock()) as mock_set, \
         patch.object(cog, "_auto_create_events", new=AsyncMock()), \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()) as mock_trigger:
        await EventsCog._poll_notion.coro(cog)

    mock_trigger.assert_awaited_once_with(events)
    mock_set.assert_awaited_once()
    # The new snapshot must include both pages
    saved_snapshot = mock_set.call_args.args[3]
    assert "page-b" in saved_snapshot


@pytest.mark.asyncio
async def test_poll_notion_notion_failure_no_snapshot_update() -> None:
    """_poll_notion: when list_events raises, snapshot is NOT updated and ERR is posted."""
    from bot.cogs.events import EventsCog

    import httpx
    cog = _make_cog_with_bot()
    cog.ctx.notion_client.list_events = AsyncMock(side_effect=httpx.RequestError("timeout"))

    with patch("bot.cogs.events.get_value", new=AsyncMock()) as mock_get, \
         patch("bot.cogs.events.set_value", new=AsyncMock()) as mock_set, \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()) as mock_trigger:
        await EventsCog._poll_notion.coro(cog)

    mock_get.assert_not_called()  # never reached snapshot comparison
    mock_set.assert_not_called()
    mock_trigger.assert_not_called()
    cog.ctx.log_service.post_error.assert_awaited_once()
    assert cog.ctx.log_service.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE


@pytest.mark.asyncio
async def test_poll_notion_db_unavailable_skips() -> None:
    """_poll_notion: when _guard_db returns False, Notion is never queried."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    # DB returns error
    cog.ctx.db.fetchval = AsyncMock(side_effect=asyncpg.PostgresConnectionError("gone"))

    with patch("bot.cogs.events.get_value", new=AsyncMock()) as mock_get:
        await EventsCog._poll_notion.coro(cog)

    cog.ctx.notion_client.list_events.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_poll_notion_notion_client_none_posts_error() -> None:
    """_poll_notion: when notion_client is None, AttributeError is caught and ERR posted."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.notion_client = None  # simulate startup race

    with patch("bot.cogs.events.get_value", new=AsyncMock()) as mock_get, \
         patch("bot.cogs.events.set_value", new=AsyncMock()) as mock_set:
        await EventsCog._poll_notion.coro(cog)

    mock_get.assert_not_called()
    mock_set.assert_not_called()
    cog.ctx.log_service.post_error.assert_awaited_once()
    assert cog.ctx.log_service.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE


# ---------------------------------------------------------------------------
# Story 3.2: _trigger_lineup_update
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trigger_lineup_update_calls_lineup_service() -> None:
    """_trigger_lineup_update calls lineup_service.post_update when service is set."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    lineup_service = MagicMock()
    lineup_service.post_update = AsyncMock()
    cog.ctx.lineup_service = lineup_service

    events = _make_pages("page-1")
    await cog._trigger_lineup_update(events)

    lineup_service.post_update.assert_awaited_once_with(events)


@pytest.mark.asyncio
async def test_trigger_lineup_update_skips_when_service_none() -> None:
    """_trigger_lineup_update does nothing when lineup_service is None."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.lineup_service = None

    events = _make_pages("page-1")
    # Should not raise
    await cog._trigger_lineup_update(events)


# ---------------------------------------------------------------------------
# Story 7.1: _auto_create_events
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

    with patch("bot.cogs.events.get_value", new=AsyncMock(side_effect=fake_get_value)), \
         patch("bot.services.event_defaults.get_value",
               new=AsyncMock(side_effect=fake_get_value)):
        yield


@pytest.mark.asyncio
async def test_auto_create_events_creates_correct_count() -> None:
    cog = _make_cog_with_bot()
    events = [
        _make_event_with_date("page-a", "2026-06-07"),
        _make_event_with_date("page-b", "2026-06-14"),
        _make_event_with_date("page-c", "2026-06-21"),
    ]
    _wire_client(cog)
    with _cfg("6", "7", "6"), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events(events, 99999)
    assert cog.ctx.notion_client.create_event.await_count == 3


@pytest.mark.asyncio
async def test_auto_create_events_skips_when_at_lookahead() -> None:
    cog = _make_cog_with_bot()
    events = [_make_event_with_date(f"page-{i}", f"2026-06-{i+1:02d}") for i in range(6)]
    _wire_client(cog)
    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value="6")):
        await cog._auto_create_events(events, 99999)
    cog.ctx.notion_client.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_auto_create_events_id_format_dates_and_configured_shape() -> None:
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
         patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events(events, 99999)

    calls = cog.ctx.notion_client.create_event.await_args_list
    assert [c.args for c in calls] == [
        ("2026-06-21 Rave Night", datetime.date(2026, 6, 21), datetime.time(19, 15), 5, 50),
        ("2026-06-28 Rave Night", datetime.date(2026, 6, 28), datetime.time(19, 15), 5, 50),
    ]


@pytest.mark.asyncio
async def test_auto_create_events_ignores_latest_event_shape_when_config_unset() -> None:
    cog = _make_cog_with_bot()
    events = [
        make_event(page_id="b", date=datetime.date(2026, 6, 14), start=datetime.time(21, 30),
                   slot_minutes=45),
    ]
    _wire_client(cog)
    with _cfg("2", "7", None), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events(events, 99999)
    args = cog.ctx.notion_client.create_event.await_args.args
    assert args[2:] == (datetime.time(20, 0), 6, 60)


@pytest.mark.asyncio
async def test_auto_create_events_defaults_without_existing_events() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("1", "7", "6"), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events([], 99999)
    args = cog.ctx.notion_client.create_event.await_args.args
    assert args[2] == datetime.time(20, 0)
    assert args[3:] == (6, 60)
    assert args[0].endswith(" Rave Night")


@pytest.mark.asyncio
async def test_auto_create_events_inits_slot_locks_with_page_id() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog, create=AsyncMock(return_value=_new_event("created-page")))
    with _cfg("2", "7", "4"), \
         patch("bot.cogs.events.init_event_slots", new=AsyncMock()) as init:
        await cog._auto_create_events([_make_event_with_date("a", "2026-06-07")], 99999)
    init.assert_awaited_once_with(cog.ctx.db, "created-page", 4)


@pytest.mark.asyncio
async def test_auto_create_events_skips_existing_id() -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog, exists=AsyncMock(side_effect=[True, False]))
    with _cfg("3", "7", "6"), patch("bot.cogs.events.init_event_slots", new=AsyncMock()) as init:
        await cog._auto_create_events([_make_event_with_date("a", "2026-06-07")], 99999)
    assert cog.ctx.notion_client.event_id_exists.await_count == 2
    assert cog.ctx.notion_client.create_event.await_count == 1
    assert cog.ctx.notion_client.create_event.await_args.args[0] == "2026-06-21 Rave Night"
    assert init.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["NotionWriteError", "DuplicateEventError"])
async def test_auto_create_events_one_failure_does_not_stop_next(error_name: str) -> None:
    from bot.notion import client as nc

    error = nc.NotionWriteError("write failed") if error_name == "NotionWriteError" \
        else nc.DuplicateEventError("dup")
    cog = _make_cog_with_bot()
    create = AsyncMock(side_effect=[error, _new_event()])
    _wire_client(cog, create=create)
    with _cfg("3", "7", "6"), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events([_make_event_with_date("a", "2026-06-07")], 99999)
    assert create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "0", "-3"])
async def test_auto_create_events_invalid_slots_config_uses_default(bad: str) -> None:
    cog = _make_cog_with_bot()
    _wire_client(cog)
    with _cfg("1", "7", bad), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events([], 99999)
    assert cog.ctx.notion_client.create_event.await_args.args[3] == 6


@pytest.mark.asyncio
async def test_auto_create_events_value_error_does_not_abort_loop() -> None:
    cog = _make_cog_with_bot()
    create = AsyncMock(side_effect=[ValueError("bad"), _new_event()])
    _wire_client(cog, create=create)
    with _cfg("3", "7", "6"), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events([_make_event_with_date("a", "2026-06-07")], 99999)
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_poll_notion_uses_list_events() -> None:
    """_poll_notion fetches the events via list_events()."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.cogs.events.set_value", new=AsyncMock()), \
         patch.object(cog, "_auto_create_events", new=AsyncMock()), \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()):
        await EventsCog._poll_notion.coro(cog)

    cog.ctx.notion_client.list_events.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_poll_notion_calls_auto_create_events() -> None:
    """_poll_notion calls _auto_create_events with fetched events and guild_id."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.cogs.events.set_value", new=AsyncMock()), \
         patch.object(cog, "_auto_create_events", new=AsyncMock()) as mock_auto, \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()):
        await EventsCog._poll_notion.coro(cog)

    mock_auto.assert_awaited_once()
    call_args = mock_auto.call_args
    assert call_args[0][0] == events  # first positional arg is events list
    assert call_args[0][1] == 99999   # second positional arg is guild_id


# ---------------------------------------------------------------------------
# Story 7.3: _auto_create_events uses slots_per_event from config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_create_events_uses_slots_per_event_from_config() -> None:
    """_auto_create_events calls init_event_slots with slots_per_event from config."""
    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # 1 event, need 2 more

    _wire_client(cog, create=AsyncMock(return_value=_new_event("new-page-id")))

    init_calls: list[tuple[object, str, int]] = []

    async def mock_init_event_slots(pool: object, event_id: str, slots_per_event: int) -> None:
        init_calls.append((pool, event_id, slots_per_event))

    with _cfg("3", "7", "3"), \
         patch("bot.cogs.events.init_event_slots", new=mock_init_event_slots):
        await cog._auto_create_events(events, 99999)

    # Two events created → init_event_slots called twice with slots=3
    assert len(init_calls) == 2
    assert init_calls[0][1] == "new-page-id"
    assert init_calls[0][2] == 3
    assert init_calls[1][1] == "new-page-id"
    assert init_calls[1][2] == 3


@pytest.mark.asyncio
async def test_auto_create_events_defaults_to_6_slots_when_unset() -> None:
    """_auto_create_events uses slots_per_event=6 when config key is not set."""
    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # 1 event, need 2 more

    _wire_client(cog, create=AsyncMock(return_value=_new_event("new-page-id")))

    init_calls: list[tuple[object, str, int]] = []

    async def mock_init_event_slots(pool: object, event_id: str, slots_per_event: int) -> None:
        init_calls.append((pool, event_id, slots_per_event))

    with _cfg("3", "7", None), \
         patch("bot.cogs.events.init_event_slots", new=mock_init_event_slots):
        await cog._auto_create_events(events, 99999)

    assert len(init_calls) == 2
    assert init_calls[0][2] == 6   # default
    assert init_calls[1][2] == 6


# ---------------------------------------------------------------------------
# Notion rework Task 13: sync cycle before the lineup, wait for ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_notion_runs_sync_cycle_before_list_and_snapshot() -> None:
    """_poll_notion runs NotionSyncService.run_cycle first, then reads fresh events."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    order: list[str] = []

    async def _sync(guild_id: int) -> list:
        order.append(f"sync:{guild_id}")
        return []

    async def _list() -> list[EventInfo]:
        order.append("list")
        return events

    async def _get(*_args: object) -> None:
        order.append("snapshot")

    cog.ctx.notion_sync_service.run_cycle = AsyncMock(side_effect=_sync)
    cog.ctx.notion_client.list_events = AsyncMock(side_effect=_list)

    with patch("bot.cogs.events.get_value", new=AsyncMock(side_effect=_get)), \
         patch("bot.cogs.events.set_value", new=AsyncMock()), \
         patch.object(cog, "_auto_create_events", new=AsyncMock()), \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()) as mock_trigger:
        await EventsCog._poll_notion.coro(cog)

    assert order == ["sync:99999", "list", "snapshot"]
    mock_trigger.assert_awaited_once_with(events)


@pytest.mark.asyncio
async def test_poll_notion_sync_failure_does_not_stop_lineup_update() -> None:
    """A failing run_cycle is logged and posted; the lineup update still runs."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    cog.ctx.notion_sync_service.run_cycle = AsyncMock(side_effect=RuntimeError("boom"))
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.cogs.events.set_value", new=AsyncMock()) as mock_set, \
         patch.object(cog, "_auto_create_events", new=AsyncMock()) as mock_auto, \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()) as mock_trigger:
        await EventsCog._poll_notion.coro(cog)

    mock_auto.assert_awaited_once()
    mock_trigger.assert_awaited_once_with(events)
    mock_set.assert_awaited_once()
    cog.ctx.log_service.post_error.assert_awaited_once()
    kwargs = cog.ctx.log_service.post_error.call_args.kwargs
    assert kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE
    assert "boom" in kwargs["trigger"]


@pytest.mark.asyncio
async def test_poll_notion_without_sync_service_still_updates_lineup() -> None:
    """notion_sync_service=None skips the sync step; the poll works as before."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.notion_sync_service = None
    events = _make_pages("page-a")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.cogs.events.set_value", new=AsyncMock()), \
         patch.object(cog, "_auto_create_events", new=AsyncMock()), \
         patch.object(cog, "_trigger_lineup_update", new=AsyncMock()) as mock_trigger:
        await EventsCog._poll_notion.coro(cog)

    mock_trigger.assert_awaited_once_with(events)
    cog.ctx.log_service.post_error.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("loop_name", ["_poll_notion", "_check_reminders"])
async def test_loops_wait_until_ready_before_first_iteration(loop_name: str) -> None:
    """Both loops register a before_loop hook that waits for the bot to be ready."""
    from bot.cogs.events import EventsCog

    loop = getattr(EventsCog, loop_name)
    assert loop._before_loop is not None

    cog = _make_cog_with_bot()
    cog.bot.wait_until_ready = AsyncMock()
    await loop._before_loop(cog)

    cog.bot.wait_until_ready.assert_awaited_once()
