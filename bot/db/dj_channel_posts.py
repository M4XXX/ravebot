"""DJ channel lineup posts: the last lineup post the bot made per channel."""
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class LastLineupPost:
    channel_id: int
    event_id: str
    message_id: int


async def get_last_post(pool: asyncpg.Pool, channel_id: int) -> LastLineupPost | None:
    row = await pool.fetchrow(
        "SELECT channel_id, event_id, message_id FROM dj_channel_lineup_posts"
        " WHERE channel_id = $1",
        channel_id,
    )
    if row is None:
        return None
    return LastLineupPost(
        channel_id=int(row["channel_id"]),
        event_id=row["event_id"],
        message_id=int(row["message_id"]),
    )


async def set_last_post(
    pool: asyncpg.Pool, channel_id: int, event_id: str, message_id: int
) -> None:
    await pool.execute(
        """
        INSERT INTO dj_channel_lineup_posts (channel_id, event_id, message_id, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (channel_id) DO UPDATE
            SET event_id = EXCLUDED.event_id,
                message_id = EXCLUDED.message_id,
                updated_at = EXCLUDED.updated_at
        """,
        channel_id,
        event_id,
        message_id,
    )
