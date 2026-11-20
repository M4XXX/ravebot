"""Real-SQL checks of bot/db/issues.py."""
import datetime

import pytest

from bot.db import issues, mirror


async def test_open_conflict_once_per_entity(pool) -> None:
    first = await issues.open_conflict(pool, "event", "u1", "both changed", {"a": 1}, {"a": 2})
    assert isinstance(first, int)
    assert await issues.open_conflict(pool, "event", "u1", "again", None, None) is None
    other = await issues.open_conflict(pool, "event", "u2", "both changed", None, None)
    assert other is not None and other != first
    assert await mirror.open_issue_keys(pool) == {("event", "u1"), ("event", "u2")}
    (row, _) = await issues.list_open(pool)
    assert (row.kind, row.entity, row.entity_key) == ("conflict", "event", "u1")
    assert (row.pg_payload, row.notion_payload) == ({"a": 1}, {"a": 2})
    resolved = await issues.resolve(pool, first, "postgres")
    assert resolved is not None and resolved.resolution == "postgres"
    assert resolved.resolved_at is not None
    assert await issues.resolve(pool, first, "notion") is None  # already closed
    # a new conflict for the same entity may open once the old one is closed
    assert await issues.open_conflict(pool, "event", "u1", "again", None, None) is not None


async def test_warnings_do_not_stall(pool) -> None:
    wid = await issues.add_warning(pool, "page deleted", "dj", "42", {"dj_name": "X"})
    await issues.add_warning(pool, "page deleted", "dj", "42")
    nid = await issues.add_warning(pool, "Notion empty")
    assert await mirror.open_issue_keys(pool) == set()
    rows = await issues.list_open(pool)
    assert [r.id for r in rows][0] == wid and len(rows) == 3
    assert rows[0].notion_payload == {"dj_name": "X"} and rows[0].kind == "warning"
    assert (rows[2].entity, rows[2].entity_key) == (None, None)
    await issues.resolve(pool, nid, "resynced")
    assert len(await issues.list_open(pool)) == 2
    with pytest.raises(ValueError):
        await issues.resolve(pool, wid, "maybe")  # type: ignore[arg-type]


async def test_expired_conflicts(pool) -> None:
    old = await issues.open_conflict(pool, "slot", "u1:0", "old", None, None)
    await issues.open_conflict(pool, "slot", "u1:1", "new", None, None)
    await issues.add_warning(pool, "old warning", "slot", "u1:2")
    await pool.execute(
        "UPDATE sync_issues SET created_at = now() - interval '25 hours'"
        " WHERE id = $1 OR kind = 'warning'",
        old,
    )
    expired = await issues.expired_conflicts(pool, datetime.timedelta(hours=24))
    assert [r.id for r in expired] == [old]
    await issues.resolve(pool, old, "expired")
    assert await issues.expired_conflicts(pool, datetime.timedelta(hours=24)) == []
