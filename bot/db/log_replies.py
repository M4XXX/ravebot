"""Log reply repository: maps a log channel message to the user who wrote it.

Keyed by the Discord id of the log channel message. The Reply button on a
"Message from ..." entry reads the mapping to know whom to answer.
"""
import asyncpg


async def save_log_reply(pool: asyncpg.Pool, message_id: int, user_id: int) -> None:
    """Store which user the log message ``message_id`` belongs to."""
    await pool.execute(
        """
        INSERT INTO log_replies (message_id, user_id)
        VALUES ($1, $2)
        ON CONFLICT (message_id) DO UPDATE SET user_id = $2
        """,
        message_id,
        user_id,
    )


async def get_log_reply_user(pool: asyncpg.Pool, message_id: int) -> int | None:
    """Return the user id for a log message, or None when there is no mapping."""
    row = await pool.fetchrow(
        "SELECT user_id FROM log_replies WHERE message_id = $1",
        message_id,
    )
    return None if row is None else row["user_id"]
