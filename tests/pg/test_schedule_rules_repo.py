"""Real-SQL checks of bot/db/schedule_rules.py."""
import datetime

from bot.db.schedule_rules import (
    delete_rule,
    get_default,
    get_rule,
    insert_rule,
    list_rules,
    set_default,
    update_rule,
)
from bot.services.schedule_rules import Rule

D = datetime.date


async def test_insert_and_list_in_order(pool) -> None:
    b = await insert_rule(pool, 1, Rule(None, "location", "Club", 2, D(2026, 1, 9)))
    a = await insert_rule(pool, 1, Rule(None, "genre", "Techno", 1, D(2026, 1, 2)))
    assert a.id and b.id and a.id != b.id
    assert await list_rules(pool, 1) == [a, b]
    assert await get_rule(pool, 1, a.id) == a


async def test_update_changes_fields(pool) -> None:
    r = await insert_rule(pool, 1, Rule(None, "genre", "Techno", 1, D(2026, 1, 2)))
    new = Rule(r.id, "genre", "House", 4, D(2026, 2, 6))
    assert await update_rule(pool, 1, new) is True
    assert await get_rule(pool, 1, r.id) == new
    assert await update_rule(pool, 2, new) is False


async def test_delete_true_then_false(pool) -> None:
    r = await insert_rule(pool, 1, Rule(None, "genre", "Techno", 1, D(2026, 1, 2)))
    assert await delete_rule(pool, 1, r.id) is True
    assert await delete_rule(pool, 1, r.id) is False
    assert await get_rule(pool, 1, r.id) is None


async def test_other_guild_rules_not_listed(pool) -> None:
    r = await insert_rule(pool, 1, Rule(None, "genre", "Techno", 1, D(2026, 1, 2)))
    assert await list_rules(pool, 2) == []
    assert await get_rule(pool, 2, r.id) is None
    assert await delete_rule(pool, 2, r.id) is False


async def test_defaults(pool) -> None:
    assert await get_default(pool, 1, "location") is None
    await set_default(pool, 1, "location", "Club")
    assert await get_default(pool, 1, "location") == "Club"
    assert await get_default(pool, 1, "genre") is None
    await set_default(pool, 1, "location", None)
    assert await get_default(pool, 1, "location") is None


async def test_save_schedule_updates_inserts_deletes_and_writes_defaults(pool) -> None:
    from bot.db.schedule_rules import save_schedule

    keep = await insert_rule(pool, 1, Rule(None, "genre", "Techno", 1, D(2026, 1, 2)))
    drop = await insert_rule(pool, 1, Rule(None, "location", "Club", 2, D(2026, 1, 9)))
    other = await insert_rule(pool, 2, Rule(None, "genre", "Other", 1, D(2026, 1, 2)))
    saved = await save_schedule(
        pool,
        1,
        [
            Rule(keep.id, "genre", "House", 3, D(2026, 2, 6)),
            Rule(None, "genre", "Trance", 1, D(2026, 3, 6)),
        ],
        {"genre": "Techno", "location": None},
    )
    assert [r.id for r in saved][0] == keep.id and drop.id not in {r.id for r in saved}
    assert {(r.value, r.every) for r in saved} == {("House", 3), ("Trance", 1)}
    assert await get_default(pool, 1, "genre") == "Techno"
    assert await get_default(pool, 1, "location") is None
    assert await list_rules(pool, 2) == [other]


async def test_save_schedule_rolls_back_on_failure(pool) -> None:
    import pytest

    from bot.db.schedule_rules import save_schedule

    keep = await insert_rule(pool, 1, Rule(None, "genre", "Techno", 1, D(2026, 1, 2)))
    with pytest.raises(Exception):  # every = 0 violates the table check
        await save_schedule(
            pool, 1, [Rule(None, "genre", "Bad", 0, D(2026, 3, 6))], {"genre": "X"}
        )
    assert await list_rules(pool, 1) == [keep]
    assert await get_default(pool, 1, "genre") is None
