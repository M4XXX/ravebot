"""Fully booked repository: events already announced as fully booked.

Keyed by the Notion page id of the Events row (same key as slot_locks.event_id).
Stored in Postgres so the announcement is once per fill and survives restarts.
"""
import asyncpg


async def mark_announced(pool: asyncpg.Pool, event_id: str) -> bool:
    """Record the announcement; True only when this call inserted the row."""
    result = await pool.execute(
        """
        INSERT INTO fully_booked_events (event_id)
        VALUES ($1)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id,
    )
    return result == "INSERT 0 1"


async def clear_announced(pool: asyncpg.Pool, event_id: str) -> None:
    """Forget the announcement (a slot opened), so a refill is announced again."""
    await pool.execute("DELETE FROM fully_booked_events WHERE event_id = $1", event_id)


async def get_announced(pool: asyncpg.Pool, event_ids: list[str]) -> set[str]:
    """Return which of the given events are marked announced (one query)."""
    if not event_ids:
        return set()
    rows = await pool.fetch(
        "SELECT event_id FROM fully_booked_events WHERE event_id = ANY($1)",
        list(event_ids),
    )
    return {row["event_id"] for row in rows}
