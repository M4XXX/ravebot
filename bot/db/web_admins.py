"""web_admins repository: admins added at runtime (owners stay in the environment)."""
import asyncpg


async def list_web_admins(pool: asyncpg.Pool) -> list[int]:
    rows = await pool.fetch(
        "SELECT discord_user_id FROM web_admins ORDER BY added_at, discord_user_id"
    )
    return [row["discord_user_id"] for row in rows]


async def add_web_admin(pool: asyncpg.Pool, user_id: int) -> bool:
    """True only if a row was inserted."""
    status = await pool.execute(
        "INSERT INTO web_admins (discord_user_id) VALUES ($1) "
        "ON CONFLICT (discord_user_id) DO NOTHING",
        user_id,
    )
    return status == "INSERT 0 1"


async def remove_web_admin(pool: asyncpg.Pool, user_id: int) -> bool:
    """True only if a row was deleted."""
    status = await pool.execute("DELETE FROM web_admins WHERE discord_user_id = $1", user_id)
    return status == "DELETE 1"
