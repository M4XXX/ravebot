"""Sync issues found by the Notion poll: conflicts (stall one entity) and warnings.

A conflict always names its entity and key; at most one is open per entity (unique
index). Warnings never stall anything. Summaries are stored as plain text; callers
escape them where they are shown. Functions accept a Pool or a Connection.
"""
import datetime
import json
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

__all__ = [
    "IssueRow",
    "Resolution",
    "add_warning",
    "expired_conflicts",
    "list_open",
    "open_conflict",
    "resolve",
]

Executor = asyncpg.Pool | asyncpg.Connection
Resolution = Literal["postgres", "notion", "resynced", "expired"]

_RESOLUTIONS = ("postgres", "notion", "resynced", "expired")
_COLUMNS = (
    "id, kind, entity, entity_key, summary, pg_payload, notion_payload, created_at,"
    " resolved_at, resolution"
)


@dataclass(frozen=True)
class IssueRow:
    id: int
    kind: str
    entity: str | None
    entity_key: str | None
    summary: str
    pg_payload: dict[str, Any] | None
    notion_payload: dict[str, Any] | None
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None
    resolution: str | None


def _json(raw: Any) -> dict[str, Any] | None:
    return json.loads(raw) if isinstance(raw, str) else raw


def _dumps(payload: dict[str, Any] | None) -> str | None:
    return json.dumps(payload, sort_keys=True) if payload is not None else None


def _row(r: asyncpg.Record) -> IssueRow:
    return IssueRow(
        id=r["id"],
        kind=r["kind"],
        entity=r["entity"],
        entity_key=r["entity_key"],
        summary=r["summary"],
        pg_payload=_json(r["pg_payload"]),
        notion_payload=_json(r["notion_payload"]),
        created_at=r["created_at"],
        resolved_at=r["resolved_at"],
        resolution=r["resolution"],
    )


async def open_conflict(
    db: Executor,
    entity: str,
    key: str,
    summary: str,
    pg_payload: dict[str, Any] | None,
    notion_payload: dict[str, Any] | None,
) -> int | None:
    """Open a conflict for entity/key; None when one is already open."""
    return await db.fetchval(
        """
        INSERT INTO sync_issues (kind, entity, entity_key, summary, pg_payload, notion_payload)
        VALUES ('conflict', $1, $2, $3, $4::jsonb, $5::jsonb)
        ON CONFLICT (entity, entity_key) WHERE resolved_at IS NULL AND kind = 'conflict'
        DO NOTHING
        RETURNING id
        """,
        entity,
        key,
        summary,
        _dumps(pg_payload),
        _dumps(notion_payload),
    )


async def add_warning(
    db: Executor,
    summary: str,
    entity: str | None = None,
    key: str | None = None,
    notion_payload: dict[str, Any] | None = None,
) -> int:
    issue_id = await db.fetchval(
        """
        INSERT INTO sync_issues (kind, entity, entity_key, summary, notion_payload)
        VALUES ('warning', $1, $2, $3, $4::jsonb)
        RETURNING id
        """,
        entity,
        key,
        summary,
        _dumps(notion_payload),
    )
    assert issue_id is not None  # INSERT ... RETURNING always yields the id
    return issue_id


async def list_open(db: Executor) -> list[IssueRow]:
    rows = await db.fetch(
        f"SELECT {_COLUMNS} FROM sync_issues WHERE resolved_at IS NULL ORDER BY created_at, id"
    )
    return [_row(r) for r in rows]


async def resolve(db: Executor, issue_id: int, resolution: Resolution) -> IssueRow | None:
    """Close an open issue; None when it does not exist or is already closed."""
    if resolution not in _RESOLUTIONS:
        raise ValueError(f"unknown resolution: {resolution}")
    row = await db.fetchrow(
        f"""
        UPDATE sync_issues SET resolved_at = now(), resolution = $2
        WHERE id = $1 AND resolved_at IS NULL
        RETURNING {_COLUMNS}
        """,
        issue_id,
        resolution,
    )
    return _row(row) if row is not None else None


async def expired_conflicts(db: Executor, older_than: datetime.timedelta) -> list[IssueRow]:
    """Open conflicts created more than ``older_than`` ago, oldest first."""
    rows = await db.fetch(
        f"""
        SELECT {_COLUMNS} FROM sync_issues
        WHERE kind = 'conflict' AND resolved_at IS NULL AND created_at < now() - $1::interval
        ORDER BY created_at, id
        """,
        older_than,
    )
    return [_row(r) for r in rows]
