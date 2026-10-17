"""Tests for bot/cogs/events.py — DB resilience guard pattern and Story 3.1 poll logic."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.ui.embeds import ErrorCode


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
    cog = EventsCog(ctx, MagicMock())

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
    cog = EventsCog(ctx, MagicMock())

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
    cog = EventsCog(ctx, MagicMock())

    result = await cog._guard_db("test_op")

    assert result is True


@pytest.mark.asyncio
async def test_guard_db_no_log_service_does_not_crash_when_db_none() -> None:
    """_guard_db handles None log_service gracefully when DB pool is also None."""
    from bot.cogs.events import EventsCog

    ctx = make_ctx(db=None, log=None)
    cog = EventsCog(ctx, MagicMock())

    result = await cog._guard_db("test_op")

    assert result is False


# ---------------------------------------------------------------------------
# Story 3.1: _build_snapshot, _get_guild_id, _poll_notion
# ---------------------------------------------------------------------------

def _make_pages(*ids: str) -> list[dict]:
    return [
        {"id": pid, "last_edited_time": f"2026-06-{i+1:02d}T00:00:00.000Z"}
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
    notion.poll_events = AsyncMock()
    ctx = MagicMock()
    ctx.db = mock_db
    ctx.log_service = log
    ctx.notion_client = notion

    cog = EventsCog(ctx, MagicMock())
    mock_bot = MagicMock()
    mock_bot.guilds = guilds if guilds is not None else [MagicMock(id=99999)]
    # disnake sets .bot as a plain attribute via _inject; set directly on the instance
    cog.bot = mock_bot
    return cog


def test_build_snapshot_stable() -> None:
    """_build_snapshot produces identical output regardless of input event order."""
    from bot.cogs.events import EventsCog

    events_a = [
        {"id": "page-z", "last_edited_time": "2026-06-02T00:00:00.000Z"},
        {"id": "page-a", "last_edited_time": "2026-06-01T00:00:00.000Z"},
    ]
    events_b = list(reversed(events_a))

    snap_a = EventsCog._build_snapshot(events_a)
    snap_b = EventsCog._build_snapshot(events_b)

    assert snap_a == snap_b
    # Verify structure: sorted by id ascending
    parsed = json.loads(snap_a)
    assert parsed[0]["id"] == "page-a"
    assert parsed[1]["id"] == "page-z"


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

    cog.ctx.notion_client.poll_events = AsyncMock(return_value=events)

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

    cog.ctx.notion_client.poll_events = AsyncMock(return_value=events)

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
    """_poll_notion: when poll_events raises, snapshot is NOT updated and ERR is posted."""
    from bot.cogs.events import EventsCog

    import httpx
    cog = _make_cog_with_bot()
    cog.ctx.notion_client.poll_events = AsyncMock(side_effect=httpx.RequestError("timeout"))

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

    cog.ctx.notion_client.poll_events.assert_not_called()
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

    events = [{"id": "page-1", "last_edited_time": "2026-06-01T00:00:00.000Z"}]
    await cog._trigger_lineup_update(events)

    lineup_service.post_update.assert_awaited_once_with(events)


@pytest.mark.asyncio
async def test_trigger_lineup_update_skips_when_service_none() -> None:
    """_trigger_lineup_update does nothing when lineup_service is None."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.lineup_service = None

    events = [{"id": "page-1", "last_edited_time": "2026-06-01T00:00:00.000Z"}]
    # Should not raise
    await cog._trigger_lineup_update(events)


# ---------------------------------------------------------------------------
# Story 7.1: _auto_create_events
# ---------------------------------------------------------------------------


def _make_event_with_date(event_id: str, date_str: str) -> dict:
    """Build a minimal Notion event dict with a Date property."""
    return {
        "id": event_id,
        "last_edited_time": "2026-06-01T00:00:00.000Z",
        "properties": {"Date": {"date": {"start": date_str}}},
    }


@pytest.mark.asyncio
async def test_auto_create_events_creates_correct_count() -> None:
    """_auto_create_events creates (lookahead - len(events)) new events."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = [
        _make_event_with_date("page-a", "2026-06-07"),
        _make_event_with_date("page-b", "2026-06-14"),
        _make_event_with_date("page-c", "2026-06-21"),
    ]
    cog.ctx.notion_client.create_event = AsyncMock(return_value="new-id")

    with patch(
        "bot.cogs.events.get_value",
        new=AsyncMock(side_effect=["6", "7", "6"]),  # lookahead=6, interval=7, slots=6
    ), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events(events, 99999)

    # 6 - 3 = 3 events to create
    assert cog.ctx.notion_client.create_event.await_count == 3


@pytest.mark.asyncio
async def test_auto_create_events_skips_when_at_lookahead() -> None:
    """_auto_create_events does nothing when event count meets look-ahead."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = [_make_event_with_date(f"page-{i}", f"2026-06-{i+1:02d}") for i in range(6)]
    cog.ctx.notion_client.create_event = AsyncMock(return_value="new-id")

    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value="6")):
        await cog._auto_create_events(events, 99999)

    cog.ctx.notion_client.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_auto_create_events_uses_interval_from_config() -> None:
    """_auto_create_events schedules new events at configured interval after latest."""
    import datetime
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # 1 event, lookahead=3
    created_dates: list[datetime.date] = []

    async def capture_create(event_name: str, event_date: datetime.date) -> str:
        created_dates.append(event_date)
        return "new-id"

    cog.ctx.notion_client.create_event = capture_create

    with patch(
        "bot.cogs.events.get_value",
        new=AsyncMock(side_effect=["3", "7", "6"]),  # lookahead=3, interval=7, slots=6
    ), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        await cog._auto_create_events(events, 99999)

    assert len(created_dates) == 2
    assert created_dates[0] == datetime.date(2026, 6, 14)   # 2026-06-07 + 7 days
    assert created_dates[1] == datetime.date(2026, 6, 21)   # 2026-06-07 + 14 days


@pytest.mark.asyncio
async def test_auto_create_events_continues_on_notion_write_error() -> None:
    """_auto_create_events does not crash on NotionWriteError — continues loop."""
    from bot.cogs.events import EventsCog
    from bot.notion.client import NotionWriteError

    cog = _make_cog_with_bot()
    events = [_make_event_with_date("page-a", "2026-06-07")]  # lookahead=3 → 2 to create
    call_count = 0

    async def flaky_create(event_name: str, event_date: object) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise NotionWriteError("write failed")
        return "new-id"

    cog.ctx.notion_client.create_event = flaky_create

    with patch(
        "bot.cogs.events.get_value",
        new=AsyncMock(side_effect=["3", "7", "6"]),  # lookahead=3, interval=7, slots=6
    ), patch("bot.cogs.events.init_event_slots", new=AsyncMock()):
        # Should not raise even though first create fails
        await cog._auto_create_events(events, 99999)

    assert call_count == 2  # both iterations attempted


@pytest.mark.asyncio
async def test_poll_notion_calls_auto_create_events() -> None:
    """_poll_notion calls _auto_create_events with fetched events and guild_id."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    cog.ctx.notion_client.poll_events = AsyncMock(return_value=events)

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

    cog.ctx.notion_client.create_event = AsyncMock(return_value="new-page-id")

    init_calls: list[tuple[object, str, int]] = []

    async def mock_init_event_slots(pool: object, event_id: str, slots_per_event: int) -> None:
        init_calls.append((pool, event_id, slots_per_event))

    with patch(
        "bot.cogs.events.get_value",
        new=AsyncMock(side_effect=["3", "7", "3"]),  # lookahead=3, interval=7, slots=3
    ), patch("bot.cogs.events.init_event_slots", new=mock_init_event_slots):
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

    cog.ctx.notion_client.create_event = AsyncMock(return_value="new-page-id")

    init_calls: list[tuple[object, str, int]] = []

    async def mock_init_event_slots(pool: object, event_id: str, slots_per_event: int) -> None:
        init_calls.append((pool, event_id, slots_per_event))

    with patch(
        "bot.cogs.events.get_value",
        new=AsyncMock(side_effect=["3", "7", None]),  # slots_per_event not set → default 6
    ), patch("bot.cogs.events.init_event_slots", new=mock_init_event_slots):
        await cog._auto_create_events(events, 99999)

    assert len(init_calls) == 2
    assert init_calls[0][2] == 6   # default
    assert init_calls[1][2] == 6
