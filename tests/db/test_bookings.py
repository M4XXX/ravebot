"""Tests for bot/db/bookings.py — slot lock acquisition and release."""
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_conn(fetchrow_result: dict | None) -> MagicMock:
    """Build a mock asyncpg connection for slot_locks queries."""

    class _FakeTransaction:
        async def __aenter__(self) -> "_FakeTransaction":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.transaction = MagicMock(return_value=_FakeTransaction())
    return conn


def _make_pool(conn: MagicMock) -> MagicMock:
    """Build a mock asyncpg Pool whose acquire() yields conn."""

    class _FakeAcquire:
        async def __aenter__(self) -> MagicMock:
            return conn

        async def __aexit__(self, *args: object) -> None:
            pass

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_FakeAcquire())
    pool.execute = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_acquire_slot_lock_first_caller_returns_true() -> None:
    """Row shows discord_user_id=None (available) → acquire returns True, UPDATE called."""
    from bot.db.bookings import acquire_slot_lock

    conn = _make_conn(fetchrow_result={"discord_user_id": None})
    pool = _make_pool(conn)

    result = await acquire_slot_lock(pool, "evt-001", 0, 12345)

    assert result is True
    # INSERT ON CONFLICT called once, then SELECT FOR UPDATE, then UPDATE to claim
    assert conn.execute.await_count >= 2


@pytest.mark.asyncio
async def test_acquire_slot_lock_row_already_taken_returns_false() -> None:
    """Row shows discord_user_id=99999 (another DJ) → acquire returns False, no UPDATE."""
    from bot.db.bookings import acquire_slot_lock

    conn = _make_conn(fetchrow_result={"discord_user_id": 99999})
    pool = _make_pool(conn)

    result = await acquire_slot_lock(pool, "evt-001", 0, 12345)

    assert result is False
    # INSERT ON CONFLICT was called; SELECT FOR UPDATE was called; UPDATE to claim was NOT
    # Verify execute was called only for INSERT (not the claim UPDATE)
    execute_calls = [str(c) for c in conn.execute.call_args_list]
    update_claim_calls = [c for c in execute_calls if "SET discord_user_id" in c]
    assert len(update_claim_calls) == 0


@pytest.mark.asyncio
async def test_release_slot_lock_clears_discord_user_id() -> None:
    """release_slot_lock executes UPDATE setting discord_user_id=NULL."""
    from bot.db.bookings import release_slot_lock

    pool = MagicMock()
    pool.execute = AsyncMock()

    await release_slot_lock(pool, "evt-001", 2)

    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "discord_user_id = NULL" in sql
    assert pool.execute.call_args[0][1] == "evt-001"
    assert pool.execute.call_args[0][2] == 2


@pytest.mark.asyncio
async def test_get_slot_lock_holder_returns_user_id_when_locked() -> None:
    """Row has discord_user_id=99999 → returns 99999."""
    from bot.db.bookings import get_slot_lock_holder

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"discord_user_id": 99999})

    result = await get_slot_lock_holder(pool, "evt-001", 0)

    assert result == 99999
    pool.fetchrow.assert_awaited_once()
    sql = pool.fetchrow.call_args[0][0]
    assert "slot_locks" in sql
    assert pool.fetchrow.call_args[0][1] == "evt-001"
    assert pool.fetchrow.call_args[0][2] == 0


@pytest.mark.asyncio
async def test_get_slot_lock_holder_returns_none_when_free() -> None:
    """Row has discord_user_id=None (unclaimed) → returns None."""
    from bot.db.bookings import get_slot_lock_holder

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"discord_user_id": None})

    result = await get_slot_lock_holder(pool, "evt-001", 1)

    assert result is None


@pytest.mark.asyncio
async def test_get_slot_lock_holder_returns_none_when_row_missing() -> None:
    """fetchrow returns None (row doesn't exist) → returns None."""
    from bot.db.bookings import get_slot_lock_holder

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    result = await get_slot_lock_holder(pool, "evt-002", 3)

    assert result is None

# ---------------------------------------------------------------------------
# Story 4.4: get_dj_slot_locks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_dj_slot_locks_returns_bookings() -> None:
    """fetch returns rows → list of dicts with event_id and slot_index."""
    from bot.db.bookings import get_dj_slot_locks

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"event_id": "evt-001", "slot_index": 0},
        {"event_id": "evt-002", "slot_index": 2},
    ])

    result = await get_dj_slot_locks(pool, 12345)

    assert len(result) == 2
    assert result[0] == {"event_id": "evt-001", "slot_index": 0}
    assert result[1] == {"event_id": "evt-002", "slot_index": 2}
    pool.fetch.assert_awaited_once()
    sql = pool.fetch.call_args[0][0]
    assert "slot_locks" in sql
    assert "discord_user_id" in sql
    assert pool.fetch.call_args[0][1] == 12345


@pytest.mark.asyncio
async def test_get_dj_slot_locks_returns_empty() -> None:
    """fetch returns [] → empty list."""
    from bot.db.bookings import get_dj_slot_locks

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])

    result = await get_dj_slot_locks(pool, 99999)

    assert result == []
