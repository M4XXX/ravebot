"""Tests for bot/db/management_lineup.py."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.management_lineup import (
    LineupRecord,
    delete_record,
    get_record,
    get_record_by_message,
    list_records,
    upsert_record,
)

_ROW = {"event_id": "ev-1", "channel_id": 5, "message_ids": [10, 11], "content_hash": "h"}


@pytest.mark.asyncio
async def test_get_record_none_when_missing() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await get_record(pool, "ev-1") is None
    assert pool.fetchrow.await_args.args[1] == "ev-1"


@pytest.mark.asyncio
async def test_get_record_returns_record_with_tuple() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=_ROW)
    assert await get_record(pool, "ev-1") == LineupRecord("ev-1", 5, (10, 11), "h")


@pytest.mark.asyncio
async def test_list_records() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_ROW])
    assert await list_records(pool) == [LineupRecord("ev-1", 5, (10, 11), "h")]


@pytest.mark.asyncio
async def test_get_record_by_message_matches_any_id() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=_ROW)
    rec = await get_record_by_message(pool, 11)
    assert rec is not None and rec.event_id == "ev-1"
    sql, mid = pool.fetchrow.await_args.args
    assert "= ANY(message_ids)" in sql and mid == 11


@pytest.mark.asyncio
async def test_get_record_by_message_none() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await get_record_by_message(pool, 1) is None


@pytest.mark.asyncio
async def test_upsert_record() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    await upsert_record(pool, "ev-1", 5, (10, 11), "h")
    sql, *args = pool.execute.await_args.args
    assert "ON CONFLICT (event_id) DO UPDATE" in sql
    assert args == ["ev-1", 5, [10, 11], "h"]


@pytest.mark.asyncio
async def test_delete_record() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    await delete_record(pool, "ev-1")
    sql, event_id = pool.execute.await_args.args
    assert "DELETE FROM management_lineup_messages" in sql and event_id == "ev-1"
