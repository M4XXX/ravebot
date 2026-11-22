"""Management lineup repository: Discord messages posted per event.

Keyed by the Notion page id of the Events row. Empty message_ids means
suppressed: the event was fully booked when the admin chose "Only new", so no
message is posted until the event opens up and fills again.
"""
from collections.abc import Sequence
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class LineupRecord:
    event_id: str
    channel_id: int
    message_ids: tuple[int, ...]
    content_hash: str


_COLUMNS = "event_id, channel_id, message_ids, content_hash"


def _record(row) -> LineupRecord:
    return LineupRecord(
        event_id=row["event_id"],
        channel_id=int(row["channel_id"]),
        message_ids=tuple(int(m) for m in row["message_ids"]),
        content_hash=row["content_hash"],
    )


async def get_record(pool: asyncpg.Pool, event_id: str) -> LineupRecord | None:
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM management_lineup_messages WHERE event_id = $1",
        event_id,
    )
    return _record(row) if row is not None else None


async def list_records(pool: asyncpg.Pool) -> list[LineupRecord]:
    rows = await pool.fetch(f"SELECT {_COLUMNS} FROM management_lineup_messages")
    return [_record(row) for row in rows]


async def get_record_by_message(pool: asyncpg.Pool, message_id: int) -> LineupRecord | None:
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM management_lineup_messages WHERE $1 = ANY(message_ids)",
        message_id,
    )
    return _record(row) if row is not None else None


async def upsert_record(
    pool: asyncpg.Pool,
    event_id: str,
    channel_id: int,
    message_ids: Sequence[int],
    content_hash: str,
) -> None:
    await pool.execute(
        """
        INSERT INTO management_lineup_messages
            (event_id, channel_id, message_ids, content_hash, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (event_id) DO UPDATE
            SET channel_id = EXCLUDED.channel_id,
                message_ids = EXCLUDED.message_ids,
                content_hash = EXCLUDED.content_hash,
                updated_at = EXCLUDED.updated_at
        """,
        event_id,
        channel_id,
        list(message_ids),
        content_hash,
    )


async def delete_record(pool: asyncpg.Pool, event_id: str) -> None:
    await pool.execute("DELETE FROM management_lineup_messages WHERE event_id = $1", event_id)
