"""Unit tests for bot/db/outreach.py — outreach_sequences repository.

Tests mock the asyncpg pool to verify correct SQL is issued without a live DB.

[Source: story 5-1-gap-detection-outreach-trigger.md#Task 7]
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from bot.db.outreach import (
    get_contacted_discord_ids,
    get_sequence,
    mark_sequence_complete,
    mark_sequence_exhausted,
    reactivate_sequence,
    record_outreach_contact,
    update_last_contact_at,
    update_outreach_contact_status,
    upsert_sequence,
)


# ---------------------------------------------------------------------------
# get_sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sequence_returns_none_when_missing():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    result = await get_sequence(pool, "evt-001")
    assert result is None
    pool.fetchrow.assert_awaited_once()
    args = pool.fetchrow.call_args[0]
    assert "outreach_sequences" in args[0]
    assert args[1] == "evt-001"


@pytest.mark.asyncio
async def test_get_sequence_returns_dict_when_found():
    pool = MagicMock()
    fake_row = {"event_id": "evt-001", "status": "active", "last_contact_at": None, "created_at": None}
    pool.fetchrow = AsyncMock(return_value=fake_row)
    result = await get_sequence(pool, "evt-001")
    assert result is not None
    assert result["event_id"] == "evt-001"
    assert result["status"] == "active"
    assert result["last_contact_at"] is None


# ---------------------------------------------------------------------------
# upsert_sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_sequence_executes_insert_on_conflict():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await upsert_sequence(pool, "evt-001")
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "INSERT INTO outreach_sequences" in sql
    assert "ON CONFLICT" in sql
    assert pool.execute.call_args[0][1] == "evt-001"


@pytest.mark.asyncio
async def test_upsert_sequence_is_idempotent():
    """Two calls with the same event_id should not raise (ON CONFLICT DO NOTHING)."""
    pool = MagicMock()
    pool.execute = AsyncMock()
    await upsert_sequence(pool, "evt-001")
    await upsert_sequence(pool, "evt-001")
    assert pool.execute.await_count == 2


# ---------------------------------------------------------------------------
# mark_sequence_complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_sequence_complete_updates_status():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await mark_sequence_complete(pool, "evt-001")
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "complete" in sql
    assert "outreach_sequences" in sql
    assert pool.execute.call_args[0][1] == "evt-001"


# ---------------------------------------------------------------------------
# mark_sequence_exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_sequence_exhausted_updates_status():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await mark_sequence_exhausted(pool, "evt-002")
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "exhausted" in sql
    assert pool.execute.call_args[0][1] == "evt-002"


# ---------------------------------------------------------------------------
# update_last_contact_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_last_contact_at_calls_execute():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await update_last_contact_at(pool, "evt-003")
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "last_contact_at" in sql
    assert "NOW()" in sql
    assert pool.execute.call_args[0][1] == "evt-003"


# ---------------------------------------------------------------------------
# Story 5.2: outreach_contacts functions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_contacted_discord_ids_returns_empty():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    result = await get_contacted_discord_ids(pool, "evt-001")
    assert result == []
    pool.fetch.assert_awaited_once()
    args = pool.fetch.call_args[0]
    assert "outreach_contacts" in args[0]
    assert args[1] == "evt-001"


@pytest.mark.asyncio
async def test_get_contacted_discord_ids_returns_ids():
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[{"discord_user_id": 111}, {"discord_user_id": 222}]
    )
    result = await get_contacted_discord_ids(pool, "evt-001")
    assert result == [111, 222]


@pytest.mark.asyncio
async def test_record_outreach_contact_inserts_row():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await record_outreach_contact(pool, "evt-001", 123)
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "INSERT INTO outreach_contacts" in sql
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_record_outreach_contact_is_idempotent():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await record_outreach_contact(pool, "evt-001", 123)
    await record_outreach_contact(pool, "evt-001", 123)
    assert pool.execute.await_count == 2


@pytest.mark.asyncio
async def test_update_outreach_contact_status_calls_execute():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await update_outreach_contact_status(pool, "evt-001", 123, "accepted")
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "outreach_contacts" in sql
    assert "status" in sql


# ---------------------------------------------------------------------------
# reactivate_sequence — Story 5.3 AC3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reactivate_sequence_calls_execute():
    pool = MagicMock()
    pool.execute = AsyncMock()
    await reactivate_sequence(pool, "evt-001")
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "outreach_sequences" in sql
    assert "active" in sql
    assert pool.execute.call_args[0][1] == "evt-001"
