"""Config table repository — runtime configuration stored in PostgreSQL.

CRUD for the config(guild_id, key, value, updated_at) table.

  get_value() — implemented Story 1.3; used by AdminLogService to look up log channel
  set_value() — implemented Story 1.4 for /config slash commands

All functions accept asyncpg.Pool as first argument — never touch global state.
[Source: architecture.md#Config Storage: PostgreSQL Config Table]
[Source: architecture.md#Repository Pattern]
[Source: epics.md#Story 1.2 schema — config table: guild_id, key, value, updated_at]
"""
import asyncpg


async def get_value(pool: asyncpg.Pool, guild_id: int, key: str) -> str | None:
    """Fetch a single config value for a guild.

    Returns the value string, or None if the key is not set for this guild
    or if the stored value is SQL NULL.

    [Source: architecture.md#Repository Pattern — accepts pool as first arg]
    """
    row = await pool.fetchrow(
        "SELECT value FROM config WHERE guild_id = $1 AND key = $2",
        guild_id,
        key,
    )
    if row is None or row["value"] is None:
        return None
    return str(row["value"])


async def set_value(pool: asyncpg.Pool, guild_id: int, key: str, value: str) -> None:
    """Upsert a config value for a guild.

    Inserts a new row or updates the existing one for (guild_id, key).
    The config table has a composite PK on (guild_id, key), making ON CONFLICT safe.

    [Source: migrations/versions/0001_initial_schema.py — PrimaryKeyConstraint("guild_id", "key")]
    [Source: architecture.md#Config Storage: PostgreSQL Config Table]
    """
    await pool.execute(
        """
        INSERT INTO config (guild_id, key, value, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (guild_id, key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """,
        guild_id,
        key,
        value,
    )
