"""DM menus: the last menu message the bot sent to each user."""
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class LastMenu:
    user_id: int
    channel_id: int
    message_id: int


async def get_menu(pool: asyncpg.Pool, user_id: int) -> LastMenu | None:
    row = await pool.fetchrow(
        "SELECT user_id, channel_id, message_id FROM dm_menus WHERE user_id = $1",
        user_id,
    )
    if row is None:
        return None
    return LastMenu(
        user_id=int(row["user_id"]),
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]),
    )


async def set_menu(
    pool: asyncpg.Pool, user_id: int, channel_id: int, message_id: int
) -> None:
    await pool.execute(
        """
        INSERT INTO dm_menus (user_id, channel_id, message_id, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (user_id) DO UPDATE
            SET channel_id = EXCLUDED.channel_id,
                message_id = EXCLUDED.message_id,
                updated_at = EXCLUDED.updated_at
        """,
        user_id,
        channel_id,
        message_id,
    )
