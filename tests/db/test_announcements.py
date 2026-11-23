"""Tests for bot/db/announcements.py."""
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db import announcements as a

NOW = datetime.datetime(2026, 11, 23, tzinfo=datetime.UTC)
T = datetime.time(18, 0)


def _rule_row(id=1, days=7):
    return {"id": id, "days_before": days, "post_time": T, "created_at": NOW}


def _post_row(**kw):
    row = {
        "id": 3, "event_id": "e1", "kind": "general", "rule_id": 1, "channel_id": 9,
        "message_id": None, "state": "claimed", "digest": "",
    }
    row.update(kw)
    return row


@pytest.mark.asyncio
async def test_list_rules_ordered() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_rule_row(1, 7), _rule_row(2, 1)])
    rules = await a.list_rules(pool, 5)
    sql, *args = pool.fetch.await_args.args
    assert "FROM announcement_rules" in sql
    assert "ORDER BY days_before DESC, post_time" in sql
    assert args == [5]
    assert rules == [a.AnnouncementRule(1, 7, T, NOW), a.AnnouncementRule(2, 1, T, NOW)]


@pytest.mark.asyncio
async def test_add_rule() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=_rule_row())
    rule = await a.add_rule(pool, 5, 7, T)
    sql, *args = pool.fetchrow.await_args.args
    assert "INSERT INTO announcement_rules" in sql
    assert "RETURNING" in sql
    assert args == [5, 7, T]
    assert rule == a.AnnouncementRule(1, 7, T, NOW)


@pytest.mark.asyncio
async def test_update_rule_and_missing() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=_rule_row(4, 2))
    rule = await a.update_rule(pool, 5, 4, 2, T)
    sql, *args = pool.fetchrow.await_args.args
    assert "UPDATE announcement_rules" in sql
    assert "RETURNING" in sql
    assert args == [5, 4, 2, T]
    assert rule.id == 4
    pool.fetchrow = AsyncMock(return_value=None)
    assert await a.update_rule(pool, 5, 4, 2, T) is None


@pytest.mark.asyncio
async def test_delete_rule() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 1")
    assert await a.delete_rule(pool, 5, 4) is True
    sql, *args = pool.execute.await_args.args
    assert "DELETE FROM announcement_rules" in sql
    assert args == [5, 4]
    pool.execute = AsyncMock(return_value="DELETE 0")
    assert await a.delete_rule(pool, 5, 4) is False


@pytest.mark.asyncio
async def test_claim_post() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=_post_row())
    post = await a.claim_post(pool, "e1", "general", 1, 9)
    sql, *args = pool.fetchrow.await_args.args
    assert "INSERT INTO announcement_posts" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "RETURNING" in sql
    assert args == ["e1", "general", 1, 9]
    assert post == a.AnnouncementPost(3, "e1", "general", 1, 9, None, "claimed", "")


@pytest.mark.asyncio
async def test_claim_post_none_when_existing() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await a.claim_post(pool, "e1", "start", None, 9) is None


@pytest.mark.asyncio
async def test_get_post_matches_null_rule() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=_post_row(kind="start", rule_id=None, message_id=77))
    post = await a.get_post(pool, "e1", "start", None)
    sql, *args = pool.fetchrow.await_args.args
    assert "COALESCE(rule_id, 0) = COALESCE($3, 0)" in sql
    assert args == ["e1", "start", None]
    assert post.rule_id is None and post.message_id == 77
    pool.fetchrow = AsyncMock(return_value=None)
    assert await a.get_post(pool, "e1", "start", None) is None


@pytest.mark.asyncio
async def test_list_posts_all_and_by_event() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_post_row()])
    posts = await a.list_posts(pool)
    sql, *args = pool.fetch.await_args.args
    assert "FROM announcement_posts" in sql and args == []
    assert len(posts) == 1
    await a.list_posts(pool, "e1")
    sql, *args = pool.fetch.await_args.args
    assert "WHERE event_id = $1" in sql
    assert args == ["e1"]


@pytest.mark.asyncio
async def test_mark_posted_set_digest_set_state_release() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    await a.mark_posted(pool, 3, 77, "d")
    sql, *args = pool.execute.await_args.args
    assert "UPDATE announcement_posts" in sql and "state = 'posted'" in sql
    assert args == [3, 77, "d"]
    await a.set_digest(pool, 3, "d2")
    sql, *args = pool.execute.await_args.args
    assert "SET digest = $2" in sql and args == [3, "d2"]
    await a.set_state(pool, 3, "gone")
    sql, *args = pool.execute.await_args.args
    assert "SET state = $2" in sql and args == [3, "gone"]
    await a.release_claim(pool, 3)
    sql, *args = pool.execute.await_args.args
    assert "DELETE FROM announcement_posts" in sql and "state = 'claimed'" in sql
    assert args == [3]
