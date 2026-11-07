"""Tests for bot/cogs/events.py — DB resilience guard pattern and Story 3.1 poll logic."""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.notion.models import EventInfo
from bot.ui.embeds import ErrorCode
from tests.notion.factories import make_event


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
    log.log_attention = AsyncMock()
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
async def test_poll_notion_refreshes_lineup_with_fetched_events() -> None:
    """_poll_notion hands the fetched events to lineup_service.refresh."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a", "page-b")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)
    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock(return_value=False))

    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)):
        await EventsCog._poll_notion.coro(cog)

    cog.ctx.lineup_service.refresh.assert_awaited_once_with(events)


@pytest.mark.asyncio
async def test_poll_notion_without_lineup_service_does_not_crash() -> None:
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.lineup_service = None
    cog.ctx.notion_client.list_events = AsyncMock(return_value=_make_pages("page-a"))

    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)):
        await EventsCog._poll_notion.coro(cog)


@pytest.mark.asyncio
async def test_poll_notion_notion_failure_no_snapshot_update() -> None:
    """_poll_notion: when list_events raises, snapshot is NOT updated and ERR is posted."""
    import httpx

    from bot.cogs.events import EventsCog
    cog = _make_cog_with_bot()
    cog.ctx.notion_client.list_events = AsyncMock(side_effect=httpx.RequestError("timeout"))

    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    await EventsCog._poll_notion.coro(cog)

    cog.ctx.lineup_service.refresh.assert_not_called()
    cog.ctx.log_service.log_attention.assert_awaited_once()
    assert cog.ctx.log_service.log_attention.call_args.args[0] == "Something failed"


@pytest.mark.asyncio
async def test_poll_notion_db_unavailable_skips() -> None:
    """_poll_notion: when _guard_db returns False, Notion is never queried."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    # DB returns error
    cog.ctx.db.fetchval = AsyncMock(side_effect=asyncpg.PostgresConnectionError("gone"))

    with patch("bot.services.event_replenish.get_value", new=AsyncMock()) as mock_get:
        await EventsCog._poll_notion.coro(cog)

    cog.ctx.notion_client.list_events.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_poll_notion_notion_client_none_posts_error() -> None:
    """_poll_notion: when notion_client is None, AttributeError is caught and ERR posted."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.notion_client = None  # simulate startup race

    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    await EventsCog._poll_notion.coro(cog)

    cog.ctx.lineup_service.refresh.assert_not_called()
    cog.ctx.log_service.log_attention.assert_awaited_once()
    assert cog.ctx.log_service.log_attention.call_args.args[0] == "Something failed"


# ---------------------------------------------------------------------------
# _auto_create_events delegates to the shared replenish function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_create_events_delegates_to_replenish_events() -> None:
    cog = _make_cog_with_bot()
    events = [make_event()]
    with patch("bot.cogs.events.replenish_events", new=AsyncMock(return_value=3)) as rep:
        result = await cog._auto_create_events(events, 99999)
    assert result == 3
    rep.assert_awaited_once_with(cog.ctx.db, cog.ctx.notion_client, 99999, events)


@pytest.mark.asyncio
async def test_poll_notion_uses_list_events() -> None:
    """_poll_notion fetches the events via list_events()."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)):
        await EventsCog._poll_notion.coro(cog)

    cog.ctx.notion_client.list_events.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_poll_notion_calls_auto_create_events() -> None:
    """_poll_notion calls _auto_create_events with fetched events and guild_id."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)) as mock_auto:
        await EventsCog._poll_notion.coro(cog)

    mock_auto.assert_awaited_once()
    call_args = mock_auto.call_args
    assert call_args[0][0] == events  # first positional arg is events list
    assert call_args[0][1] == 99999   # second positional arg is guild_id


# ---------------------------------------------------------------------------
# Notion rework Task 13: sync cycle before the lineup, wait for ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_notion_runs_sync_cycle_before_list_and_refresh() -> None:
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

    async def _refresh(_events: list[EventInfo]) -> bool:
        order.append("refresh")
        return False

    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock(side_effect=_refresh))

    cog.ctx.notion_sync_service.run_cycle = AsyncMock(side_effect=_sync)
    cog.ctx.notion_client.list_events = AsyncMock(side_effect=_list)

    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)):
        await EventsCog._poll_notion.coro(cog)

    assert order == ["sync:99999", "list", "refresh"]
    cog.ctx.lineup_service.refresh.assert_awaited_once_with(events)


@pytest.mark.asyncio
async def test_poll_notion_sync_failure_does_not_stop_lineup_update() -> None:
    """A failing run_cycle is logged and posted; the lineup update still runs."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a")
    cog.ctx.notion_sync_service.run_cycle = AsyncMock(side_effect=RuntimeError("boom"))
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)) as mock_auto:
        await EventsCog._poll_notion.coro(cog)

    mock_auto.assert_awaited_once()
    cog.ctx.lineup_service.refresh.assert_awaited_once_with(events)
    cog.ctx.log_service.log_attention.assert_awaited_once()
    title, what, _todo = cog.ctx.log_service.log_attention.call_args.args
    assert title == "Something failed"
    assert "boom" not in what


@pytest.mark.asyncio
async def test_poll_notion_without_sync_service_still_updates_lineup() -> None:
    """notion_sync_service=None skips the sync step; the poll works as before."""
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.notion_sync_service = None
    events = _make_pages("page-a")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)

    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)):
        await EventsCog._poll_notion.coro(cog)

    cog.ctx.lineup_service.refresh.assert_awaited_once_with(events)
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


@pytest.mark.asyncio
async def test_poll_notion_refresh_failure_does_not_raise() -> None:
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.notion_client.list_events = AsyncMock(return_value=_make_pages("a"))
    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock(side_effect=TypeError("boom")))
    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)):
        await EventsCog._poll_notion.coro(cog)
    cog.ctx.lineup_service.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_notion_refresh_reloads_when_events_were_created() -> None:
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.notion_client.list_events = AsyncMock(return_value=_make_pages("a"))
    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=2)):
        await EventsCog._poll_notion.coro(cog)
    cog.ctx.lineup_service.refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_poll_notion_runs_fully_booked_check_for_all_events() -> None:
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    events = _make_pages("page-a", "page-b")
    cog.ctx.notion_client.list_events = AsyncMock(return_value=events)
    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock(return_value=False))

    with patch.object(cog, "_auto_create_events", new=AsyncMock(return_value=0)), \
         patch("bot.cogs.events.check_fully_booked", new=AsyncMock()) as check:
        await EventsCog._poll_notion.coro(cog)

    args = check.await_args
    assert args.args == (cog.ctx.db, cog.ctx.log_service, events)
    assert isinstance(args.kwargs["listed_at"], datetime.datetime)
