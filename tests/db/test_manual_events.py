"""Tests for bot/db/manual_events.py."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_mark_manual_is_idempotent() -> None:
    from bot.db.manual_events import mark_manual

    pool = MagicMock()
    pool.execute = AsyncMock()
    await mark_manual(pool, "page-1")
    sql, page_id = pool.execute.await_args.args
    assert "INSERT INTO manual_events" in sql and "ON CONFLICT" in sql and "DO NOTHING" in sql
    assert page_id == "page-1"


@pytest.mark.asyncio
async def test_get_manual_page_ids_returns_only_marked() -> None:
    from bot.db.manual_events import get_manual_page_ids

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"page_id": "a"}])
    assert await get_manual_page_ids(pool, ["a", "b"]) == {"a"}
    sql, ids = pool.fetch.await_args.args
    assert "FROM manual_events" in sql and "ANY" in sql and ids == ["a", "b"]


@pytest.mark.asyncio
async def test_get_manual_page_ids_empty_input_does_no_query() -> None:
    from bot.db.manual_events import get_manual_page_ids

    pool = MagicMock()
    pool.fetch = AsyncMock()
    assert await get_manual_page_ids(pool, []) == set()
    pool.fetch.assert_not_awaited()
