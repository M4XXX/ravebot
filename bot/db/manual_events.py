"""manual_events repository: events created by hand, ignored by the event scheduler.

Keyed by the Notion page id of the Events row.
"""
import asyncpg


async def mark_manual(pool: asyncpg.Pool, page_id: str) -> None:
    """Record the event as manual (idempotent)."""
    await pool.execute(
        "INSERT INTO manual_events (page_id) VALUES ($1) ON CONFLICT (page_id) DO NOTHING",
        page_id,
    )


async def get_manual_page_ids(pool: asyncpg.Pool, page_ids: list[str]) -> set[str]:
    """The given page ids that are manual events (one query; none for empty input)."""
    if not page_ids:
        return set()
    rows = await pool.fetch(
        "SELECT page_id FROM manual_events WHERE page_id = ANY($1::text[])", page_ids
    )
    return {row["page_id"] for row in rows}
