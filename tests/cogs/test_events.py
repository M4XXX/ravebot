"""Tests for bot/cogs/events.py — DB resilience guard pattern."""
from unittest.mock import AsyncMock, MagicMock

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
