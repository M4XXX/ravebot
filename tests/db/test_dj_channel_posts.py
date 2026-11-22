"""Tests for bot/db/dj_channel_posts.py."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.dj_channel_posts import LastLineupPost, get_last_post, set_last_post


@pytest.mark.asyncio
async def test_get_last_post_none_when_missing() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await get_last_post(pool, 5) is None
    assert pool.fetchrow.await_args.args[1] == 5


@pytest.mark.asyncio
async def test_get_last_post_returns_record_with_ints() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={"channel_id": 5, "event_id": "ev-1", "message_id": 99}
    )
    assert await get_last_post(pool, 5) == LastLineupPost(5, "ev-1", 99)


@pytest.mark.asyncio
async def test_set_last_post_upserts_on_channel_id() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    await set_last_post(pool, 5, "ev-1", 99)
    sql, *args = pool.execute.await_args.args
    assert "INSERT INTO dj_channel_lineup_posts" in sql
    assert "ON CONFLICT (channel_id) DO UPDATE" in sql
    assert args == [5, "ev-1", 99]
