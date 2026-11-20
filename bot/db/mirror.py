"""Notion mirror state: per entity, the Notion page id and the last synced payload.

Functions accept a Pool or a Connection.
"""
import datetime
import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from bot.sync.payloads import payload_hash

__all__ = ["MirrorRow", "forget", "get", "get_all", "open_issue_keys", "set_synced"]

Executor = asyncpg.Pool | asyncpg.Connection

_COLUMNS = "entity, entity_key, notion_page_id, synced_hash, synced_payload, synced_at"


@dataclass(frozen=True)
class MirrorRow:
    entity: str
    entity_key: str
    notion_page_id: str | None
    synced_hash: str | None
    synced_payload: dict[str, Any] | None
    synced_at: datetime.datetime | None


def _row(r: asyncpg.Record) -> MirrorRow:
    raw = r["synced_payload"]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    return MirrorRow(
        entity=r["entity"],
        entity_key=r["entity_key"],
        notion_page_id=r["notion_page_id"],
        synced_hash=r["synced_hash"],
        synced_payload=payload,
        synced_at=r["synced_at"],
    )


async def get(db: Executor, entity: str, key: str) -> MirrorRow | None:
    row = await db.fetchrow(
        f"SELECT {_COLUMNS} FROM notion_mirror WHERE entity = $1 AND entity_key = $2",
        entity,
        key,
    )
    return _row(row) if row is not None else None


async def get_all(db: Executor, entity: str) -> dict[str, MirrorRow]:
    rows = await db.fetch(
        f"SELECT {_COLUMNS} FROM notion_mirror WHERE entity = $1", entity
    )
    return {r["entity_key"]: _row(r) for r in rows}


async def set_synced(
    db: Executor, entity: str, key: str, page_id: str, payload: dict[str, Any]
) -> None:
    """Record that Notion page ``page_id`` now holds exactly ``payload``."""
    await db.execute(
        """
        INSERT INTO notion_mirror
            (entity, entity_key, notion_page_id, synced_hash, synced_payload, synced_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, now())
        ON CONFLICT (entity, entity_key) DO UPDATE SET
            notion_page_id = EXCLUDED.notion_page_id,
            synced_hash = EXCLUDED.synced_hash,
            synced_payload = EXCLUDED.synced_payload,
            synced_at = EXCLUDED.synced_at
        """,
        entity,
        key,
        page_id,
        payload_hash(payload),
        json.dumps(payload, sort_keys=True),
    )


async def forget(db: Executor, entity: str, key: str) -> None:
    await db.execute(
        "DELETE FROM notion_mirror WHERE entity = $1 AND entity_key = $2", entity, key
    )


async def open_issue_keys(db: Executor) -> set[tuple[str, str]]:
    """(entity, key) of every open conflict; warnings never stall an entity."""
    rows = await db.fetch(
        "SELECT entity, entity_key FROM sync_issues"
        " WHERE kind = 'conflict' AND resolved_at IS NULL"
        " AND entity IS NOT NULL AND entity_key IS NOT NULL"
    )
    return {(r["entity"], r["entity_key"]) for r in rows}
