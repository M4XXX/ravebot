"""Real-SQL checks of bot/db/announcements.py."""
import datetime

import asyncpg
import pytest

from bot.db import announcements as db_ann

G = 222222222222222222
CHANNEL = 333333333333333333


async def test_claim_post_twice_returns_the_post_then_none(pool) -> None:  # noqa: ANN001
    rule = await db_ann.add_rule(pool, G, 3, datetime.time(18, 0))
    first = await db_ann.claim_post(pool, "e1", "general", rule.id, CHANNEL)
    assert first is not None and first.state == "claimed"
    assert await db_ann.claim_post(pool, "e1", "general", rule.id, CHANNEL) is None


async def test_start_kind_is_unique_per_event(pool) -> None:  # noqa: ANN001
    assert await db_ann.claim_post(pool, "e1", "start", None, CHANNEL) is not None
    assert await db_ann.claim_post(pool, "e1", "start", None, CHANNEL) is None
    assert await db_ann.claim_post(pool, "e2", "start", None, CHANNEL) is not None


async def test_other_kinds_and_rules_of_one_event_are_fine(pool) -> None:  # noqa: ANN001
    r1 = await db_ann.add_rule(pool, G, 3, datetime.time(18, 0))
    r2 = await db_ann.add_rule(pool, G, 1, datetime.time(12, 0))
    assert await db_ann.claim_post(pool, "e1", "start", None, CHANNEL) is not None
    assert await db_ann.claim_post(pool, "e1", "general", r1.id, CHANNEL) is not None
    assert await db_ann.claim_post(pool, "e1", "general", r2.id, CHANNEL) is not None
    assert len(await db_ann.list_posts(pool, "e1")) == 3


async def test_deleting_a_rule_keeps_its_posts(pool) -> None:  # noqa: ANN001
    rule = await db_ann.add_rule(pool, G, 3, datetime.time(18, 0))
    await db_ann.claim_post(pool, "e1", "general", rule.id, CHANNEL)
    assert await db_ann.delete_rule(pool, G, rule.id) is True
    posts = await db_ann.list_posts(pool, "e1")
    assert [p.rule_id for p in posts] == [rule.id]


async def test_rule_check_refuses_366_days(pool) -> None:  # noqa: ANN001
    with pytest.raises(asyncpg.CheckViolationError):
        await db_ann.add_rule(pool, G, 366, datetime.time(18, 0))
    assert (await db_ann.add_rule(pool, G, 365, datetime.time(18, 0))).days_before == 365


async def test_get_post_finds_a_post_with_a_null_rule(pool) -> None:  # noqa: ANN001
    await db_ann.claim_post(pool, "e1", "start", None, CHANNEL)
    post = await db_ann.get_post(pool, "e1", "start", None)
    assert post is not None and post.rule_id is None
    assert await db_ann.get_post(pool, "e2", "start", None) is None
