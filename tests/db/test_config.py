"""Tests for bot/db/config.py — config table repository."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_get_value_returns_value() -> None:
    """get_value() returns the string value when the key exists."""
    mock_pool = MagicMock()
    mock_pool.fetchrow = AsyncMock(return_value={"value": "123456789"})
    from bot.db.config import get_value

    result = await get_value(mock_pool, guild_id=1, key="log_channel")

    assert result == "123456789"
    mock_pool.fetchrow.assert_awaited_once_with(
        "SELECT value FROM config WHERE guild_id = $1 AND key = $2",
        1,
        "log_channel",
    )


@pytest.mark.asyncio
async def test_get_value_returns_none_when_row_missing() -> None:
    """get_value() returns None when no row exists for the key."""
    mock_pool = MagicMock()
    mock_pool.fetchrow = AsyncMock(return_value=None)
    from bot.db.config import get_value

    result = await get_value(mock_pool, guild_id=1, key="nonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_get_value_returns_none_when_value_is_null() -> None:
    """get_value() returns None when the row exists but value is SQL NULL."""
    mock_pool = MagicMock()
    mock_pool.fetchrow = AsyncMock(return_value={"value": None})
    from bot.db.config import get_value

    result = await get_value(mock_pool, guild_id=1, key="log_channel")

    assert result is None


@pytest.mark.asyncio
async def test_get_value_uses_correct_guild_id() -> None:
    """get_value() passes the guild_id correctly to the query."""
    mock_pool = MagicMock()
    mock_pool.fetchrow = AsyncMock(return_value=None)
    from bot.db.config import get_value

    await get_value(mock_pool, guild_id=42, key="dj_channel")

    mock_pool.fetchrow.assert_awaited_once_with(
        "SELECT value FROM config WHERE guild_id = $1 AND key = $2",
        42,
        "dj_channel",
    )


@pytest.mark.asyncio
async def test_set_value_executes_upsert_with_correct_params() -> None:
    """set_value() executes an upsert query with guild_id, key, and value."""
    mock_pool = MagicMock()
    mock_pool.execute = AsyncMock(return_value=None)
    from bot.db.config import set_value

    await set_value(mock_pool, guild_id=1, key="dj_channel", value="123456")

    mock_pool.execute.assert_awaited_once()
    call_args = mock_pool.execute.call_args[0]
    query: str = call_args[0]
    assert "INSERT INTO config" in query
    assert "ON CONFLICT" in query
    assert call_args[1] == 1
    assert call_args[2] == "dj_channel"
    assert call_args[3] == "123456"


@pytest.mark.asyncio
async def test_set_value_upsert_works_for_log_channel() -> None:
    """set_value() upsert path works for any key — e.g. log_channel."""
    mock_pool = MagicMock()
    mock_pool.execute = AsyncMock(return_value=None)
    from bot.db.config import set_value

    await set_value(mock_pool, guild_id=999, key="log_channel", value="987654")

    mock_pool.execute.assert_awaited_once()
    call_args = mock_pool.execute.call_args[0]
    assert call_args[1] == 999
    assert call_args[2] == "log_channel"
    assert call_args[3] == "987654"
