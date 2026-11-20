"""Tests for bot/db/events.py with a mocked pool (SQL shape and error mapping)."""
import datetime
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from bot.db.events import (
    DuplicateEventError,
    EventDraft,
    event_id_exists,
    insert_event,
    set_event_status,
    update_event,
)

DRAFT = EventDraft(
    event_id="RAVE-1",
    date=datetime.date(2026, 12, 1),
    start=datetime.time(20, 0),
    slot_minutes=60,
    slot_count=3,
)


def _conn(execute_side_effect=None) -> MagicMock:
    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=execute_side_effect)
    conn.executemany = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetch = AsyncMock(return_value=[])
    conn.transaction = MagicMock(return_value=_Tx())
    return conn


def _pool(conn: MagicMock) -> MagicMock:
    class _Acq:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return False

    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire = MagicMock(return_value=_Acq())
    return pool


async def test_insert_event_creates_slots() -> None:
    conn = _conn()
    event = await insert_event(_pool(conn), DRAFT, uid="u1")
    conn.transaction.assert_called_once()
    assert "INSERT INTO events" in conn.execute.await_args_list[0].args[0]
    rows = conn.executemany.await_args.args[1]
    assert rows == [("u1", 0), ("u1", 1), ("u1", 2)]
    assert event.page_id == "u1"
    assert [s.number for s in event.slots] == [1, 2, 3]
    assert all(s.discord_id is None for s in event.slots)


async def test_insert_on_connection_opens_no_transaction() -> None:
    conn = _conn()
    await insert_event(conn, DRAFT, uid="u1")
    conn.transaction.assert_not_called()


async def test_insert_duplicate_event_id_raises() -> None:
    conn = _conn(execute_side_effect=asyncpg.UniqueViolationError("dup"))
    with pytest.raises(DuplicateEventError) as exc:
        await insert_event(_pool(conn), DRAFT)
    assert exc.value.event_id == "RAVE-1"
    conn.executemany.assert_not_awaited()


async def test_update_event_rejects_unknown_field() -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        await update_event(_pool(conn), "u1", slot_count=2, bogus=1)
    conn.execute.assert_not_awaited()


async def test_update_event_sets_updated_at_and_reports_row() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")
    assert await update_event(pool, "u1", genre="House") is True
    sql = pool.execute.await_args.args[0]
    assert "genre = $2" in sql and "updated_at = now()" in sql
    pool.execute = AsyncMock(return_value="UPDATE 0")
    assert await update_event(pool, "u1", genre="House") is False


async def test_set_event_status_and_exists() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")
    pool.fetchval = AsyncMock(return_value=True)
    await set_event_status(pool, "u1", None)
    assert pool.execute.await_args.args[2] is None
    assert await event_id_exists(pool, "RAVE-1") is True
