"""Unit tests for bot/db/interventions.py -- Story 6.2.

Tests cover: is_under_intervention, start_intervention, end_intervention.
All tests use mock asyncpg.Pool following the pattern in tests/db/test_djs.py.

[Source: story 6-2-admin-intervention-take-over-dj-dm.md#Task 10]
"""
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_pool(fetchrow_return=None, execute_return=None):
    """Build a minimal mock asyncpg.Pool."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.execute = AsyncMock(return_value=execute_return)
    return pool


@pytest.mark.asyncio
async def test_is_under_intervention_returns_false_when_no_row():
    """is_under_intervention returns False when no row exists."""
    from bot.db.interventions import is_under_intervention

    pool = _make_pool(fetchrow_return=None)
    result = await is_under_intervention(pool, discord_user_id=123)

    assert result is False
    pool.fetchrow.assert_awaited_once_with(
        "SELECT 1 FROM interventions WHERE discord_user_id = $1",
        123,
    )


@pytest.mark.asyncio
async def test_is_under_intervention_returns_true_when_row_exists():
    """is_under_intervention returns True when a row is present."""
    from bot.db.interventions import is_under_intervention

    pool = _make_pool(fetchrow_return=MagicMock())  # any non-None row
    result = await is_under_intervention(pool, discord_user_id=456)

    assert result is True


@pytest.mark.asyncio
async def test_start_intervention_executes_insert():
    """start_intervention executes INSERT ... ON CONFLICT DO NOTHING."""
    from bot.db.interventions import start_intervention

    pool = _make_pool()
    await start_intervention(pool, discord_user_id=789)

    pool.execute.assert_awaited_once_with(
        "INSERT INTO interventions (discord_user_id) VALUES ($1) ON CONFLICT DO NOTHING",
        789,
    )


@pytest.mark.asyncio
async def test_start_intervention_idempotent():
    """Calling start_intervention twice does not raise (ON CONFLICT DO NOTHING)."""
    from bot.db.interventions import start_intervention

    pool = _make_pool()
    await start_intervention(pool, discord_user_id=111)
    await start_intervention(pool, discord_user_id=111)

    assert pool.execute.await_count == 2  # called twice, neither raises


@pytest.mark.asyncio
async def test_end_intervention_executes_delete():
    """end_intervention executes DELETE for the given user."""
    from bot.db.interventions import end_intervention

    pool = _make_pool()
    await end_intervention(pool, discord_user_id=999)

    pool.execute.assert_awaited_once_with(
        "DELETE FROM interventions WHERE discord_user_id = $1",
        999,
    )


@pytest.mark.asyncio
async def test_end_intervention_no_op_when_not_present():
    """end_intervention does not raise if the DJ was not under intervention."""
    from bot.db.interventions import end_intervention

    pool = _make_pool()
    # Simply verifying no exception is raised when DELETE affects 0 rows
    await end_intervention(pool, discord_user_id=404)
    pool.execute.assert_awaited_once()
