"""setup_drafts repository: the open setup wizard of a guild (one row per guild)."""
import json
from dataclasses import dataclass, field
from typing import Any

import asyncpg


@dataclass
class SetupDraft:
    guild_id: int
    message_id: int
    channel_id: int
    step: int
    data: dict[str, Any] = field(default_factory=dict)


def _load(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


async def get_draft(pool: asyncpg.Pool, guild_id: int) -> SetupDraft | None:
    row = await pool.fetchrow(
        "SELECT guild_id, message_id, channel_id, step, data "
        "FROM setup_drafts WHERE guild_id = $1",
        guild_id,
    )
    if row is None:
        return None
    return SetupDraft(
        guild_id=row["guild_id"],
        message_id=row["message_id"],
        channel_id=row["channel_id"],
        step=row["step"],
        data=_load(row["data"]),
    )


async def save_draft(
    pool: asyncpg.Pool,
    guild_id: int,
    message_id: int,
    channel_id: int,
    step: int,
    data: dict[str, Any],
) -> None:
    await pool.execute(
        """
        INSERT INTO setup_drafts (guild_id, message_id, channel_id, step, data, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
        ON CONFLICT (guild_id) DO UPDATE
            SET message_id = EXCLUDED.message_id,
                channel_id = EXCLUDED.channel_id,
                step = EXCLUDED.step,
                data = EXCLUDED.data,
                updated_at = EXCLUDED.updated_at
        """,
        guild_id,
        message_id,
        channel_id,
        step,
        json.dumps(data),
    )


async def delete_draft(pool: asyncpg.Pool, guild_id: int) -> None:
    await pool.execute("DELETE FROM setup_drafts WHERE guild_id = $1", guild_id)
