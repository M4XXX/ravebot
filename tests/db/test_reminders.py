"""Unit tests for bot/db/reminders.py.

Tests use mocked asyncpg pools — no real DB connection required.

[Source: story 5-4-day-before-event-reminder.md#Tasks 9]
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.reminders import has_reminder_been_sent, record_reminder_sent


@pytest.mark.asyncio
async def test_has_reminder_been_sent_returns_false_when_no_row():
    """fetchval returns False → function returns False."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)

    result = await has_reminder_been_sent(pool, "evt-001", 123, 0)

    assert result is False
    pool.fetchval.assert_awaited_once()
    sql = pool.fetchval.call_args[0][0]
    assert "reminders_sent" in sql
    assert "SELECT EXISTS" in sql


@pytest.mark.asyncio
async def test_has_reminder_been_sent_returns_true_when_row_exists():
    """fetchval returns True → function returns True."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=True)

    result = await has_reminder_been_sent(pool, "evt-001", 123, 0)

    assert result is True


@pytest.mark.asyncio
async def test_has_reminder_been_sent_passes_correct_args():
    """Correct positional args forwarded to fetchval."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)

    await has_reminder_been_sent(pool, "evt-XYZ", 999, 2)

    args = pool.fetchval.call_args[0]
    assert args[1] == "evt-XYZ"
    assert args[2] == 999
    assert args[3] == 2


@pytest.mark.asyncio
async def test_record_reminder_sent_calls_execute():
    """execute called with INSERT INTO reminders_sent and correct positional args."""
    pool = MagicMock()
    pool.execute = AsyncMock()

    await record_reminder_sent(pool, "evt-001", 123, 0)

    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "reminders_sent" in sql
    assert "INSERT" in sql
    # Positional args: sql, event_id, discord_user_id, slot_index
    args = pool.execute.call_args[0]
    assert args[1] == "evt-001"
    assert args[2] == 123
    assert args[3] == 0


@pytest.mark.asyncio
async def test_record_reminder_sent_uses_on_conflict_do_nothing():
    """SQL must include ON CONFLICT DO NOTHING for idempotency."""
    pool = MagicMock()
    pool.execute = AsyncMock()

    await record_reminder_sent(pool, "evt-002", 456, 1)

    sql = pool.execute.call_args[0][0]
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
