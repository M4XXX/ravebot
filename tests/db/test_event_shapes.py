"""Tests for bot/db/event_shapes.py and migration 0008."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_get_event_shapes_empty_ids_skips_query() -> None:
    from bot.db.event_shapes import get_event_shapes

    pool = MagicMock()
    pool.fetch = AsyncMock()
    assert await get_event_shapes(pool, []) == {}
    pool.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_event_shapes_returns_rows_keyed_by_event_id() -> None:
    from bot.db.event_shapes import EventShapeRow, get_event_shapes

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"event_id": "ev-1", "start": "20:00", "slot_minutes": 60, "slot_count": 6},
        {"event_id": "ev-2", "start": "18:30", "slot_minutes": 90, "slot_count": 3},
    ])
    shapes = await get_event_shapes(pool, ["ev-1", "ev-2", "ev-3"])
    assert shapes == {
        "ev-1": EventShapeRow("ev-1", "20:00", 60, 6),
        "ev-2": EventShapeRow("ev-2", "18:30", 90, 3),
    }
    sql, ids = pool.fetch.await_args.args
    assert "FROM event_shapes" in sql and "ANY($1)" in sql
    assert ids == ["ev-1", "ev-2", "ev-3"]


@pytest.mark.asyncio
async def test_upsert_event_shape_inserts_or_updates() -> None:
    from bot.db.event_shapes import upsert_event_shape

    pool = MagicMock()
    pool.execute = AsyncMock()
    await upsert_event_shape(pool, "ev-1", "20:00", 60, 6)
    sql, *args = pool.execute.await_args.args
    assert "INSERT INTO event_shapes" in sql
    assert "ON CONFLICT (event_id) DO UPDATE" in sql
    assert "updated_at = NOW()" in sql
    assert args == ["ev-1", "20:00", 60, 6]


def test_migration_0008_creates_and_drops_event_shapes() -> None:
    path = Path(__file__).parents[2] / "migrations/versions/0008_event_shapes.py"
    spec = importlib.util.spec_from_file_location("mig0008", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0008", "0007")
    with patch.object(mod, "op") as op:
        mod.upgrade()
    op.create_table.assert_called_once()
    args = op.create_table.call_args.args
    assert args[0] == "event_shapes"
    columns = {c.name: c for c in args[1:] if hasattr(c, "nullable")}
    assert set(columns) == {"event_id", "start", "slot_minutes", "slot_count", "updated_at"}
    assert all(not c.nullable for c in columns.values())
    assert columns["updated_at"].server_default is not None
    pk = [c for c in args[1:] if type(c).__name__ == "PrimaryKeyConstraint"]
    assert len(pk) == 1 and list(pk[0]._pending_colargs) == ["event_id"]
    with patch.object(mod, "op") as op:
        mod.downgrade()
    op.drop_table.assert_called_once_with("event_shapes")
