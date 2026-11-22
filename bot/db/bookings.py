"""Slot booking repository — pessimistic locking for concurrent slot writes.

Handles: FR17, FR19, FR23 — slot booking writes with SELECT...FOR UPDATE.

CRITICAL PATTERN:
  acquire_slot_lock() uses SELECT...FOR UPDATE on the existing slot row within a single
  transaction to guarantee only one DJ can claim a given (event_id, slot_index) pair.
  slot_locks is the bookings table (Postgres is the source of truth); Notion only
  mirrors it. event_id is the event uid (EventInfo.page_id).

[Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
"""
import datetime
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class SlotLock:
    """One slot_locks row: the holder (None = unclaimed) and when it was booked."""

    discord_user_id: int | None
    booked_at: datetime.datetime | None


_LOCK_SLOT_ROW = """
SELECT s.discord_user_id FROM slot_locks s
JOIN events e ON e.uid = s.event_id AND s.slot_index < e.slot_count
WHERE s.event_id = $1 AND s.slot_index = $2
FOR UPDATE OF s
"""


async def _lock_slot_row(
    conn: asyncpg.Connection, event_id: str, slot_index: int
) -> asyncpg.Record | None:
    """Lock the existing slot row of an existing event (None: the slot is gone).

    The one row lock every booking change takes (DJ booking, admin override), so
    they are serialized; no row is ever created here.
    """
    return await conn.fetchrow(_LOCK_SLOT_ROW, event_id, slot_index)


class DjSlotLimitReached(Exception):
    """The DJ already holds ``max_per_dj`` slots in this event; nothing was written."""


# Serializes the limited claims of one DJ in one event (transaction-scoped, released
# on commit or rollback). Two-int key space: (event, DJ); a hash collision only adds
# serialization, never a wrong answer.
_LOCK_DJ_IN_EVENT = "SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2::bigint::text))"

_COUNT_DJ_SLOTS = """
SELECT count(*) FROM slot_locks s
JOIN events e ON e.uid = s.event_id AND s.slot_index < e.slot_count
WHERE s.event_id = $1 AND s.discord_user_id = $2 AND s.slot_index <> $3
"""


async def acquire_slot_lock(
    pool: asyncpg.Pool,
    event_id: str,
    slot_index: int,
    discord_user_id: int,
    max_per_dj: int | None = None,
) -> bool:
    """Try to claim a slot using SELECT...FOR UPDATE inside a transaction.

    Returns True if the slot was successfully claimed by this DJ.
    Returns False if the slot is already taken by another DJ, or when it no longer
    exists (event deleted, or ``slot_index`` at or above the event's slot count).

    Algorithm:
      1. SELECT ... FOR UPDATE OF the existing slot row, joined to its event and
         limited to the event's slot count. No row is ever created here, so a stale
         booking never revives a removed slot. Locks the row; blocks concurrent
         callers (an event edit that deletes the row makes the waiter see none).
      2. Check discord_user_id: if not NULL, slot is taken -> return False.
      3. ``max_per_dj`` set (DJ-facing bookings): take a transaction-scoped advisory
         lock on (event, DJ), then count the DJ's other slots of this event (limited
         to its slot count). At or above ``max_per_dj`` -> raise DjSlotLimitReached
         before anything is written. The advisory lock makes two parallel claims of
         different slots by the same DJ wait for each other, so the second count sees
         the first claim (READ COMMITTED: each statement takes a fresh snapshot).
         ``None`` (Notion intake) runs no count.
      4. UPDATE to set discord_user_id -> commit -> return True.

    [Source: epics.md#Story 4.2 AC2]
    [Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Step 1: lock the existing slot row of an existing event
            row = await _lock_slot_row(conn, event_id, slot_index)
            if row is None or row["discord_user_id"] is not None:
                # The slot no longer exists, or it is already taken
                return False
            # Step 3: the per-event limit, serialized per (event, DJ)
            if max_per_dj is not None:
                await conn.execute(_LOCK_DJ_IN_EVENT, event_id, discord_user_id)
                held = await conn.fetchval(
                    _COUNT_DJ_SLOTS, event_id, discord_user_id, slot_index
                )
                if held >= max_per_dj:
                    raise DjSlotLimitReached(f"{event_id}:{discord_user_id}")
            # Step 4: claim the slot within the same transaction
            await conn.execute(
                """
                UPDATE slot_locks
                SET discord_user_id = $3, booked_at = NOW()
                WHERE event_id = $1 AND slot_index = $2
                """,
                event_id,
                slot_index,
                discord_user_id,
            )
            return True


async def get_slot_lock_holder(
    pool: asyncpg.Pool,
    event_id: str,
    slot_index: int,
) -> int | None:
    """Return the discord_user_id currently holding the slot lock, or None.

    Returns None if the slot row does not exist or discord_user_id is NULL (unclaimed).
    Used by BookingConfirmView to include both DJ IDs in ERR_SLOT_CONFLICT log entries.

    [Source: epics.md#Story 4.3 AC2]
    """
    row = await pool.fetchrow(
        "SELECT discord_user_id FROM slot_locks WHERE event_id = $1 AND slot_index = $2",
        event_id,
        slot_index,
    )
    if row is None:
        return None
    return row["discord_user_id"]


async def release_slot_lock(
    pool: asyncpg.Pool,
    event_id: str,
    slot_index: int,
    expected_holder: int | None = None,
) -> bool:
    """Release a previously claimed slot lock — sets discord_user_id back to NULL.

    Returns True when a held slot was freed (False: nobody, or someone else, held it).

    Called when a DJ self-cancels (Story 4.4) or an admin cancels.

    ``expected_holder`` set: only release if that user still holds the slot, so a
    stale caller never frees a slot someone else took in the meantime.

    Idempotent: safe to call even if the row does not exist or is already NULL.

    [Source: epics.md#Story 4.4 (cancellation flow)]
    """
    if expected_holder is None:
        result = await pool.execute(
            """
            UPDATE slot_locks
            SET discord_user_id = NULL, booked_at = NULL
            WHERE event_id = $1 AND slot_index = $2 AND discord_user_id IS NOT NULL
            """,
            event_id,
            slot_index,
        )
        return result == "UPDATE 1"
    result = await pool.execute(
        """
        UPDATE slot_locks
        SET discord_user_id = NULL, booked_at = NULL
        WHERE event_id = $1 AND slot_index = $2 AND discord_user_id = $3
        """,
        event_id,
        slot_index,
        expected_holder,
    )
    return result == "UPDATE 1"


async def get_dj_slot_locks(
    pool: asyncpg.Pool,
    discord_user_id: int,
) -> list[dict]:
    """Return all (event_id, slot_index) pairs currently held by this DJ.

    Used by the cancellation flow to find the DJ's upcoming booked slots.
    Returns a list of dicts: [{"event_id": str, "slot_index": int}, ...]
    Returns empty list if the DJ has no active bookings.

    [Source: epics.md#Story 4.4 AC1]
    [Source: architecture.md#Slot Booking Atomicity]
    """
    rows = await pool.fetch(
        "SELECT event_id, slot_index FROM slot_locks WHERE discord_user_id = $1"
        " ORDER BY booked_at NULLS LAST, event_id, slot_index",
        discord_user_id,
    )
    return [{"event_id": row["event_id"], "slot_index": row["slot_index"]} for row in rows]


class SlotGoneError(Exception):
    """The slot does not exist: its event was deleted or has fewer slots now."""


async def force_slot_holder(
    pool: asyncpg.Pool,
    event_id: str,
    slot_index: int,
    discord_user_id: int | None,
) -> int | None:
    """Set the slot's holder to ``discord_user_id`` (None frees it); admin override only.

    Takes the same row lock as acquire_slot_lock (SELECT ... FOR UPDATE of the existing
    slot row, joined to its event and limited to its slot count), so a DJ booking racing
    the override is serialized with it. Like acquire_slot_lock it never creates a row:
    raises ``SlotGoneError`` when the slot no longer exists. Returns the previous holder
    (None if the slot was free).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _lock_slot_row(conn, event_id, slot_index)
            if row is None:
                raise SlotGoneError(f"{event_id}:{slot_index}")
            previous = row["discord_user_id"]
            if previous != discord_user_id:
                await conn.execute(
                    """
                    UPDATE slot_locks
                    SET discord_user_id = $3,
                        booked_at = CASE WHEN $3::bigint IS NULL THEN NULL ELSE NOW() END
                    WHERE event_id = $1 AND slot_index = $2
                    """,
                    event_id,
                    slot_index,
                    discord_user_id,
                )
            return previous


async def init_event_slots(
    pool: asyncpg.Pool | asyncpg.Connection,
    event_id: str,
    slots_per_event: int,
) -> None:
    """Pre-initialize slot_locks rows for a new event, all with discord_user_id = NULL.

    Inserts one row per slot index (0 to slots_per_event-1).
    ON CONFLICT DO NOTHING — idempotent; safe to call multiple times.
    Called by EventsCog._auto_create_events() after a successful create_event() call.

    [Source: epics.md#Story 7.3 AC2 — FR37]
    [Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
    """
    if slots_per_event <= 0:
        return
    rows = [(event_id, i) for i in range(slots_per_event)]
    await pool.executemany(
        """
        INSERT INTO slot_locks (event_id, slot_index)
        VALUES ($1, $2)
        ON CONFLICT (event_id, slot_index) DO NOTHING
        """,
        rows,
    )


async def get_booked_djs_for_event(
    pool: asyncpg.Pool,
    event_id: str,
) -> list[dict]:
    """Return all DJs with an active booking for this event.

    Joins slot_locks with djs to include DJ display name alongside booking details.
    Only returns rows where slot_locks.discord_user_id IS NOT NULL (active bookings).

    Returns list of dicts: [{"event_id", "slot_index", "discord_user_id", "dj_name"}, ...]
    Returns empty list if no slots are booked for this event.

    Used by RemindersService to find DJs who need day-before reminders.

    [Source: epics.md#Story 5.4 AC1]
    [Source: architecture.md#Repository Pattern]
    """
    rows = await pool.fetch(
        """
        SELECT sl.event_id, sl.slot_index, sl.discord_user_id, d.dj_name
        FROM slot_locks sl
        JOIN djs d ON d.discord_user_id = sl.discord_user_id
        WHERE sl.event_id = $1 AND sl.discord_user_id IS NOT NULL
        ORDER BY sl.slot_index ASC
        """,
        event_id,
    )
    return [
        {
            "event_id": row["event_id"],
            "slot_index": row["slot_index"],
            "discord_user_id": row["discord_user_id"],
            "dj_name": row["dj_name"],
        }
        for row in rows
    ]


async def get_slot_locks_for_events(
    pool: asyncpg.Pool,
    event_ids: list[str],
) -> dict[tuple[str, int], SlotLock]:
    """Return every slot_locks row of the given events, keyed by (event_id, slot_index).

    One query. Rows without a holder are included (discord_user_id None).
    """
    if not event_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT event_id, slot_index, discord_user_id, booked_at
        FROM slot_locks
        WHERE event_id = ANY($1)
        """,
        list(event_ids),
    )
    return {
        (row["event_id"], row["slot_index"]): SlotLock(
            discord_user_id=row["discord_user_id"], booked_at=row["booked_at"]
        )
        for row in rows
    }
