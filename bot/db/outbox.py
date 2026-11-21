"""Notion outbox: one row per entity whose state still has to reach Notion.

Rows coalesce on (entity, entity_key): a later change replaces a pending one
(upsert replaces delete and vice versa) and resets the retry state. The mirror
worker reads due rows, writes Notion and calls ``done`` (only removes the row if
nothing was enqueued meanwhile) or ``failed`` (backoff).
"""
import datetime
from dataclasses import dataclass

import asyncpg

__all__ = ["OutboxRow", "OutboxStats", "done", "due", "enqueue", "failed", "stats"]

Executor = asyncpg.Pool | asyncpg.Connection

MAX_BACKOFF_SECONDS = 3600
BASE_BACKOFF_SECONDS = 10


@dataclass(frozen=True)
class OutboxRow:
    entity: str
    entity_key: str
    op: str
    notion_page_id: str | None
    enqueued_at: datetime.datetime
    attempts: int
    next_attempt_at: datetime.datetime
    last_error: str | None


@dataclass(frozen=True)
class OutboxStats:
    count: int
    oldest: datetime.datetime | None
    last_error: str | None


async def enqueue(
    pool: Executor,
    entity: str,
    key: str,
    op: str = "upsert",
    notion_page_id: str | None = None,
) -> None:
    """Queue (or re-queue) the entity; the newest operation wins."""
    if op not in ("upsert", "delete"):
        raise ValueError(f"unknown outbox op: {op}")
    # clock_timestamp(), not now(): two enqueues in one transaction must differ,
    # so done() of the older one cannot remove the newer one.
    await pool.execute(
        """
        INSERT INTO notion_outbox
            (entity, entity_key, op, notion_page_id, enqueued_at, attempts,
             next_attempt_at, last_error)
        VALUES ($1, $2, $3, $4, clock_timestamp(), 0, now(), NULL)
        ON CONFLICT (entity, entity_key) DO UPDATE SET
            op = EXCLUDED.op,
            notion_page_id = COALESCE(EXCLUDED.notion_page_id, notion_outbox.notion_page_id),
            enqueued_at = EXCLUDED.enqueued_at,
            attempts = 0,
            next_attempt_at = EXCLUDED.next_attempt_at,
            last_error = NULL
        """,
        entity,
        key,
        op,
        notion_page_id,
    )


def _row(r: asyncpg.Record) -> OutboxRow:
    return OutboxRow(
        entity=r["entity"],
        entity_key=r["entity_key"],
        op=r["op"],
        notion_page_id=r["notion_page_id"],
        enqueued_at=r["enqueued_at"],
        attempts=r["attempts"],
        next_attempt_at=r["next_attempt_at"],
        last_error=r["last_error"],
    )


async def due(pool: Executor, limit: int) -> list[OutboxRow]:
    """Rows whose retry time has come, oldest first.

    Entities with an open conflict stay queued but are not returned, so they never
    take the places of writable rows.
    """
    rows = await pool.fetch(
        """
        SELECT o.entity, o.entity_key, o.op, o.notion_page_id, o.enqueued_at,
               o.attempts, o.next_attempt_at, o.last_error
        FROM notion_outbox o
        WHERE o.next_attempt_at <= now()
          AND NOT EXISTS (
              SELECT 1 FROM sync_issues i
              WHERE i.kind = 'conflict' AND i.resolved_at IS NULL
                AND i.entity = o.entity AND i.entity_key = o.entity_key
          )
        ORDER BY o.next_attempt_at, o.enqueued_at
        LIMIT $1
        """,
        limit,
    )
    return [_row(r) for r in rows]


async def done(
    pool: Executor, entity: str, key: str, enqueued_at: datetime.datetime
) -> None:
    """Remove the row, unless it was enqueued again after it was read."""
    await pool.execute(
        "DELETE FROM notion_outbox"
        " WHERE entity = $1 AND entity_key = $2 AND enqueued_at = $3",
        entity,
        key,
        enqueued_at,
    )


async def failed(
    pool: Executor,
    entity: str,
    key: str,
    error: str,
    enqueued_at: datetime.datetime | None = None,
) -> None:
    """Count the attempt and retry after min(2**attempts * 10 s, 1 h).

    With ``enqueued_at`` (the value the worker read) the row is left alone when it was
    enqueued again during the failed write: the newer change is retried at once.
    """
    await pool.execute(
        """
        UPDATE notion_outbox SET
            attempts = attempts + 1,
            next_attempt_at = now() + make_interval(
                secs => LEAST(power(2, LEAST(attempts, 20)) * $4, $5)
            ),
            last_error = $3
        WHERE entity = $1 AND entity_key = $2
          AND ($6::timestamptz IS NULL OR enqueued_at = $6)
        """,
        entity,
        key,
        error,
        BASE_BACKOFF_SECONDS,
        MAX_BACKOFF_SECONDS,
        enqueued_at,
    )


async def stats(pool: Executor) -> OutboxStats:
    row = await pool.fetchrow(
        """
        SELECT count(*) AS count, min(enqueued_at) AS oldest,
               (SELECT last_error FROM notion_outbox
                WHERE last_error IS NOT NULL
                -- newest failure: the retry time minus the backoff that failure set
                ORDER BY next_attempt_at - make_interval(
                    secs => LEAST(power(2, LEAST(attempts - 1, 20)) * $1, $2)
                ) DESC
                LIMIT 1) AS last_error
        FROM notion_outbox
        """,
        BASE_BACKOFF_SECONDS,
        MAX_BACKOFF_SECONDS,
    )
    if row is None:
        return OutboxStats(count=0, oldest=None, last_error=None)
    return OutboxStats(count=row["count"], oldest=row["oldest"], last_error=row["last_error"])
