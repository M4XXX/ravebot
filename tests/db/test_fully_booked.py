"""Tests for bot/db/fully_booked.py and migration 0009."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_mark_announced_true_when_row_inserted() -> None:
    from bot.db.fully_booked import mark_announced

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    assert await mark_announced(pool, "ev-1") is True
    sql, event_id = pool.execute.await_args.args
    assert "INSERT INTO fully_booked_events" in sql
    assert "ON CONFLICT (event_id) DO NOTHING" in sql
    assert event_id == "ev-1"


@pytest.mark.asyncio
async def test_mark_announced_false_on_conflict() -> None:
    from bot.db.fully_booked import mark_announced

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 0")
    assert await mark_announced(pool, "ev-1") is False


@pytest.mark.asyncio
async def test_clear_announced_deletes_row() -> None:
    from bot.db.fully_booked import clear_announced

    pool = MagicMock()
    pool.execute = AsyncMock()
    await clear_announced(pool, "ev-1")
    sql, event_id = pool.execute.await_args.args
    assert "DELETE FROM fully_booked_events" in sql
    assert event_id == "ev-1"


@pytest.mark.asyncio
async def test_get_announced_returns_stored_subset() -> None:
    from bot.db.fully_booked import get_announced

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"event_id": "ev-1"}, {"event_id": "ev-3"}])
    assert await get_announced(pool, ["ev-1", "ev-2", "ev-3"]) == {"ev-1", "ev-3"}
    sql, ids = pool.fetch.await_args.args
    assert "FROM fully_booked_events" in sql and "ANY($1)" in sql
    assert ids == ["ev-1", "ev-2", "ev-3"]


@pytest.mark.asyncio
async def test_get_announced_empty_ids_skips_query() -> None:
    from bot.db.fully_booked import get_announced

    pool = MagicMock()
    pool.fetch = AsyncMock()
    assert await get_announced(pool, []) == set()
    pool.fetch.assert_not_awaited()


def test_migration_0009_creates_and_drops_fully_booked_events() -> None:
    path = Path(__file__).parents[2] / "migrations/versions/0009_fully_booked_events.py"
    spec = importlib.util.spec_from_file_location("mig0009", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0009", "0008")
    with patch.object(mod, "op") as op:
        mod.upgrade()
    args = op.create_table.call_args.args
    assert args[0] == "fully_booked_events"
    columns = {c.name: c for c in args[1:] if hasattr(c, "nullable")}
    assert set(columns) == {"event_id", "announced_at"}
    assert all(not c.nullable for c in columns.values())
    assert columns["announced_at"].server_default is not None
    pk = [c for c in args[1:] if type(c).__name__ == "PrimaryKeyConstraint"]
    assert len(pk) == 1 and list(pk[0]._pending_colargs) == ["event_id"]
    with patch.object(mod, "op") as op:
        mod.downgrade()
    op.drop_table.assert_called_once_with("fully_booked_events")
