"""dj_conversations repository: the open onboarding/edit DM conversation per user."""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg


@dataclass
class Conversation:
    discord_user_id: int
    guild_id: int
    kind: str
    step: str
    data: dict[str, Any] = field(default_factory=dict)
    logo_image: bytes | None = None
    message_id: int | None = None
    # Last state change (set by the database on every save); only read, never written.
    updated_at: datetime | None = None


def _load(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


async def get_conversation(pool: asyncpg.Pool, user_id: int) -> Conversation | None:
    row = await pool.fetchrow(
        "SELECT discord_user_id, guild_id, kind, step, data, logo_image, message_id, "
        "updated_at "
        "FROM dj_conversations WHERE discord_user_id = $1",
        user_id,
    )
    if row is None:
        return None
    return Conversation(
        discord_user_id=row["discord_user_id"],
        guild_id=row["guild_id"],
        kind=row["kind"],
        step=row["step"],
        data=_load(row["data"]),
        logo_image=row["logo_image"],
        message_id=row["message_id"],
        updated_at=row["updated_at"],
    )


async def save_conversation(pool: asyncpg.Pool, conv: Conversation) -> None:
    await pool.execute(
        """
        INSERT INTO dj_conversations (
            discord_user_id, guild_id, kind, step, data, logo_image, message_id, updated_at
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, NOW())
        ON CONFLICT (discord_user_id) DO UPDATE
            SET guild_id = EXCLUDED.guild_id,
                kind = EXCLUDED.kind,
                step = EXCLUDED.step,
                data = EXCLUDED.data,
                logo_image = EXCLUDED.logo_image,
                message_id = EXCLUDED.message_id,
                updated_at = EXCLUDED.updated_at
        """,
        conv.discord_user_id,
        conv.guild_id,
        conv.kind,
        conv.step,
        json.dumps(conv.data),
        conv.logo_image,
        conv.message_id,
    )


async def delete_conversation(pool: asyncpg.Pool, user_id: int) -> None:
    await pool.execute(
        "DELETE FROM dj_conversations WHERE discord_user_id = $1", user_id
    )


async def set_conversation_message(
    pool: asyncpg.Pool, user_id: int, message_id: int
) -> None:
    await pool.execute(
        "UPDATE dj_conversations SET message_id = $1, updated_at = NOW() "
        "WHERE discord_user_id = $2",
        message_id,
        user_id,
    )
