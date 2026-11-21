"""Schedule rules repository and per-guild default genre/location."""
import asyncpg

from bot.db.config import get_value, set_value
from bot.services.schedule_rules import Field, Rule

DEFAULT_GENRE_KEY = "default_genre"
DEFAULT_LOCATION_KEY = "default_location"

_COLS = "id, field, value, every, anchor"
_DEFAULT_KEYS = {"genre": DEFAULT_GENRE_KEY, "location": DEFAULT_LOCATION_KEY}


def _rule(row: asyncpg.Record) -> Rule:
    return Rule(
        id=row["id"], field=row["field"], value=row["value"],
        every=row["every"], anchor=row["anchor"],
    )


async def list_rules(db: asyncpg.Pool | asyncpg.Connection, guild_id: int) -> list[Rule]:
    rows = await db.fetch(
        f"SELECT {_COLS} FROM schedule_rules WHERE guild_id = $1 ORDER BY field, anchor, id",
        guild_id,
    )
    return [_rule(r) for r in rows]


async def get_rule(
    db: asyncpg.Pool | asyncpg.Connection, guild_id: int, rule_id: int
) -> Rule | None:
    row = await db.fetchrow(
        f"SELECT {_COLS} FROM schedule_rules WHERE guild_id = $1 AND id = $2", guild_id, rule_id
    )
    return _rule(row) if row else None


async def insert_rule(db: asyncpg.Pool | asyncpg.Connection, guild_id: int, rule: Rule) -> Rule:
    row = await db.fetchrow(
        "INSERT INTO schedule_rules (guild_id, field, value, every, anchor)"
        f" VALUES ($1, $2, $3, $4, $5) RETURNING {_COLS}",
        guild_id, rule.field, rule.value, rule.every, rule.anchor,
    )
    return _rule(row)


async def update_rule(db: asyncpg.Pool | asyncpg.Connection, guild_id: int, rule: Rule) -> bool:
    result = await db.execute(
        "UPDATE schedule_rules SET field = $3, value = $4, every = $5, anchor = $6"
        " WHERE guild_id = $1 AND id = $2",
        guild_id, rule.id, rule.field, rule.value, rule.every, rule.anchor,
    )
    return result.endswith(" 1")


async def delete_rule(
    db: asyncpg.Pool | asyncpg.Connection, guild_id: int, rule_id: int
) -> bool:
    result = await db.execute(
        "DELETE FROM schedule_rules WHERE guild_id = $1 AND id = $2", guild_id, rule_id
    )
    return result.endswith(" 1")


async def get_default(db: asyncpg.Pool, guild_id: int, field: Field) -> str | None:
    return await get_value(db, guild_id, _DEFAULT_KEYS[field]) or None


async def set_default(db: asyncpg.Pool, guild_id: int, field: Field, value: str | None) -> None:
    await set_value(db, guild_id, _DEFAULT_KEYS[field], value or "")
