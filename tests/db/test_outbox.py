"""Tests for bot/db/outbox.py and bot/db/mirror.py with a mocked pool (SQL shape).

The behaviour against real Postgres is covered in tests/pg/test_outbox.py.
"""
import datetime
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db import mirror, outbox
from bot.sync.payloads import payload_hash

NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.UTC)


def _pool() -> MagicMock:
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


async def test_enqueue_coalesces() -> None:
    pool = _pool()
    await outbox.enqueue(pool, "slot", "u1:0", "delete", "page-1")
    sql, *args = pool.execute.await_args.args
    assert "ON CONFLICT (entity, entity_key) DO UPDATE" in sql
    assert "op = EXCLUDED.op" in sql
    assert "attempts = 0" in sql and "last_error = NULL" in sql
    assert "clock_timestamp()" in sql
    assert args == ["slot", "u1:0", "delete", "page-1"]


async def test_enqueue_rejects_unknown_op() -> None:
    with pytest.raises(ValueError, match="unknown outbox op"):
        await outbox.enqueue(_pool(), "event", "u1", "patch")


async def test_done_keeps_newer_enqueue() -> None:
    pool = _pool()
    await outbox.done(pool, "event", "u1", NOW)
    sql, *args = pool.execute.await_args.args
    assert sql.startswith("DELETE FROM notion_outbox")
    assert "enqueued_at = $3" in sql
    assert args == ["event", "u1", NOW]


async def test_failed_backs_off() -> None:
    pool = _pool()
    await outbox.failed(pool, "dj", "42", "boom")
    sql, *args = pool.execute.await_args.args
    assert "attempts = attempts + 1" in sql
    assert "LEAST(power(2, LEAST(attempts, 20)) * $4, $5)" in sql
    assert "enqueued_at = $6" in sql
    assert args == ["dj", "42", "boom", 10, 3600, None]


async def test_due_skips_open_conflicts_and_maps_rows() -> None:
    pool = _pool()
    pool.fetch.return_value = [{
        "entity": "event", "entity_key": "u1", "op": "upsert", "notion_page_id": None,
        "enqueued_at": NOW, "attempts": 0, "next_attempt_at": NOW, "last_error": None,
    }]
    rows = await outbox.due(pool, 5)
    sql, limit = pool.fetch.await_args.args
    assert "kind = 'conflict' AND i.resolved_at IS NULL" in sql
    assert limit == 5
    assert rows == [outbox.OutboxRow("event", "u1", "upsert", None, NOW, 0, NOW, None)]


async def test_stats_maps_row() -> None:
    pool = _pool()
    pool.fetchrow.return_value = {"count": 2, "oldest": NOW, "last_error": "x"}
    assert await outbox.stats(pool) == outbox.OutboxStats(2, NOW, "x")


async def test_set_synced_stores_hash_and_payload() -> None:
    pool = _pool()
    payload = {"event_uid": "u1", "number": 1, "discord_id": None}
    await mirror.set_synced(pool, "slot", "u1:0", "page-1", payload)
    sql, *args = pool.execute.await_args.args
    assert "ON CONFLICT (entity, entity_key) DO UPDATE" in sql
    assert args[:4] == ["slot", "u1:0", "page-1", payload_hash(payload)]
    assert json.loads(args[4]) == payload


async def test_get_decodes_json_payload() -> None:
    pool = _pool()
    pool.fetchrow.return_value = {
        "entity": "dj", "entity_key": "42", "notion_page_id": "p",
        "synced_hash": "h", "synced_payload": '{"dj_name": "A"}', "synced_at": NOW,
    }
    row = await mirror.get(pool, "dj", "42")
    assert row == mirror.MirrorRow("dj", "42", "p", "h", {"dj_name": "A"}, NOW)


async def test_open_issue_keys_only_conflicts() -> None:
    pool = _pool()
    pool.fetch.return_value = [{"entity": "event", "entity_key": "u1"}]
    assert await mirror.open_issue_keys(pool) == {("event", "u1")}
    assert "kind = 'conflict'" in pool.fetch.await_args.args[0]
