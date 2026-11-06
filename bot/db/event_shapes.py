"""Event shape repository: the last accepted Start / Slot length / Slots per event.

Keyed by the Notion page id of the Events row (same key as slot_locks.event_id).
The Notion sync service compares each event with its stored shape: a changed
Start or Slot length is accepted while no slot holds a DJ, otherwise restored.
"""
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class EventShapeRow:
    """One event_shapes row; ``start`` is the text "HH:MM" as written in Notion."""

    event_id: str
    start: str
    slot_minutes: int
    slot_count: int


async def get_event_shapes(
    pool: asyncpg.Pool,
    event_ids: list[str],
) -> dict[str, EventShapeRow]:
    """Return the stored shapes of the given events, keyed by event_id (one query).

    Events without a stored row are missing from the result.
    """
    if not event_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT event_id, start, slot_minutes, slot_count
        FROM event_shapes
        WHERE event_id = ANY($1)
        """,
        list(event_ids),
    )
    return {
        row["event_id"]: EventShapeRow(
            event_id=row["event_id"],
            start=row["start"],
            slot_minutes=row["slot_minutes"],
            slot_count=row["slot_count"],
        )
        for row in rows
    }


async def upsert_event_shape(
    pool: asyncpg.Pool,
    event_id: str,
    start: str,
    slot_minutes: int,
    slot_count: int,
) -> None:
    """Store (insert or replace) the accepted shape of one event."""
    await pool.execute(
        """
        INSERT INTO event_shapes (event_id, start, slot_minutes, slot_count, updated_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (event_id) DO UPDATE
            SET start = $2, slot_minutes = $3, slot_count = $4, updated_at = NOW()
        """,
        event_id,
        start,
        slot_minutes,
        slot_count,
    )
