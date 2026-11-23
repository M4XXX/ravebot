"""Event announcements: posting rules and the posts already made."""
import datetime
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class AnnouncementRule:
    id: int
    days_before: int
    post_time: datetime.time
    created_at: datetime.datetime


@dataclass(frozen=True)
class AnnouncementPost:
    id: int
    event_id: str
    kind: str  # "general" or "start"
    rule_id: int | None
    channel_id: int
    message_id: int | None
    state: str  # claimed, posted, gone, ended, canceled
    digest: str


_RULE_COLS = "id, days_before, post_time, created_at"
_POST_COLS = "id, event_id, kind, rule_id, channel_id, message_id, state, digest"


def _rule(row) -> AnnouncementRule:
    return AnnouncementRule(
        id=int(row["id"]),
        days_before=int(row["days_before"]),
        post_time=row["post_time"],
        created_at=row["created_at"],
    )


def _post(row) -> AnnouncementPost:
    return AnnouncementPost(
        id=int(row["id"]),
        event_id=row["event_id"],
        kind=row["kind"],
        rule_id=None if row["rule_id"] is None else int(row["rule_id"]),
        channel_id=int(row["channel_id"]),
        message_id=None if row["message_id"] is None else int(row["message_id"]),
        state=row["state"],
        digest=row["digest"],
    )


async def list_rules(db: asyncpg.Pool, guild_id: int) -> list[AnnouncementRule]:
    rows = await db.fetch(
        f"SELECT {_RULE_COLS} FROM announcement_rules WHERE guild_id = $1 "
        "ORDER BY days_before DESC, post_time",
        guild_id,
    )
    return [_rule(r) for r in rows]


async def add_rule(
    db: asyncpg.Pool, guild_id: int, days_before: int, post_time: datetime.time
) -> AnnouncementRule:
    row = await db.fetchrow(
        "INSERT INTO announcement_rules (guild_id, days_before, post_time) "
        f"VALUES ($1, $2, $3) RETURNING {_RULE_COLS}",
        guild_id,
        days_before,
        post_time,
    )
    return _rule(row)


async def update_rule(
    db: asyncpg.Pool,
    guild_id: int,
    rule_id: int,
    days_before: int,
    post_time: datetime.time,
) -> AnnouncementRule | None:
    row = await db.fetchrow(
        "UPDATE announcement_rules SET days_before = $3, post_time = $4, created_at = now() "
        f"WHERE guild_id = $1 AND id = $2 RETURNING {_RULE_COLS}",
        guild_id,
        rule_id,
        days_before,
        post_time,
    )
    return None if row is None else _rule(row)


async def delete_rule(db: asyncpg.Pool, guild_id: int, rule_id: int) -> bool:
    result = await db.execute(
        "DELETE FROM announcement_rules WHERE guild_id = $1 AND id = $2",
        guild_id,
        rule_id,
    )
    return result == "DELETE 1"


async def claim_post(
    db: asyncpg.Pool, event_id: str, kind: str, rule_id: int | None, channel_id: int
) -> AnnouncementPost | None:
    row = await db.fetchrow(
        "INSERT INTO announcement_posts (event_id, kind, rule_id, channel_id) "
        "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
        f"RETURNING {_POST_COLS}",
        event_id,
        kind,
        rule_id,
        channel_id,
    )
    return None if row is None else _post(row)


async def get_post(
    db: asyncpg.Pool, event_id: str, kind: str, rule_id: int | None
) -> AnnouncementPost | None:
    row = await db.fetchrow(
        f"SELECT {_POST_COLS} FROM announcement_posts "
        "WHERE event_id = $1 AND kind = $2 AND COALESCE(rule_id, 0) = COALESCE($3, 0)",
        event_id,
        kind,
        rule_id,
    )
    return None if row is None else _post(row)


async def list_posts(
    db: asyncpg.Pool, event_id: str | None = None
) -> list[AnnouncementPost]:
    if event_id is None:
        rows = await db.fetch(f"SELECT {_POST_COLS} FROM announcement_posts ORDER BY id")
    else:
        rows = await db.fetch(
            f"SELECT {_POST_COLS} FROM announcement_posts WHERE event_id = $1 ORDER BY id",
            event_id,
        )
    return [_post(r) for r in rows]


async def mark_posted(db: asyncpg.Pool, post_id: int, message_id: int, digest: str) -> None:
    await db.execute(
        "UPDATE announcement_posts SET state = 'posted', message_id = $2, digest = $3 "
        "WHERE id = $1",
        post_id,
        message_id,
        digest,
    )


async def set_digest(db: asyncpg.Pool, post_id: int, digest: str) -> None:
    await db.execute(
        "UPDATE announcement_posts SET digest = $2 WHERE id = $1", post_id, digest
    )


async def set_state(db: asyncpg.Pool, post_id: int, state: str) -> None:
    await db.execute(
        "UPDATE announcement_posts SET state = $2 WHERE id = $1", post_id, state
    )


async def release_claim(db: asyncpg.Pool, post_id: int) -> None:
    await db.execute(
        "DELETE FROM announcement_posts WHERE id = $1 AND state = 'claimed'", post_id
    )
