"""Slot booking repository — pessimistic locking for concurrent slot writes.

Handles: FR17, FR19, FR23 — slot booking writes with SELECT...FOR UPDATE.

CRITICAL PATTERN:
  acquire_slot_lock() uses INSERT ON CONFLICT + SELECT...FOR UPDATE within a single
  transaction to guarantee only one DJ can claim a given (event_id, slot_index) pair.
  The DB lock is a semaphore only — the authoritative booking record lives in Notion.
  On Notion write failure, the caller MUST call release_slot_lock() to undo the claim.

[Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
[Source: architecture.md#Gap Analysis — PG lock vs Notion write boundary]
"""
import asyncpg


async def acquire_slot_lock(
    pool: asyncpg.Pool,
    event_id: str,
    slot_index: int,
    discord_user_id: int,
) -> bool:
    """Try to claim a slot using SELECT...FOR UPDATE inside a transaction.

    Returns True if the slot was successfully claimed by this DJ.
    Returns False if the slot is already taken by another DJ.

    Algorithm:
      1. INSERT ON CONFLICT DO NOTHING — ensures the row exists before FOR UPDATE.
      2. SELECT ... FOR UPDATE — acquires exclusive row lock; blocks concurrent callers.
      3. Check discord_user_id: if not NULL, slot is taken → return False.
      4. UPDATE to set discord_user_id → commit → return True.

    [Source: epics.md#Story 4.2 AC2]
    [Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Step 1: guarantee the row exists so FOR UPDATE has something to lock
            await conn.execute(
                """
                INSERT INTO slot_locks (event_id, slot_index)
                VALUES ($1, $2)
                ON CONFLICT (event_id, slot_index) DO NOTHING
                """,
                event_id,
                slot_index,
            )
            # Step 2: acquire exclusive row lock
            row = await conn.fetchrow(
                """
                SELECT discord_user_id FROM slot_locks
                WHERE event_id = $1 AND slot_index = $2
                FOR UPDATE
                """,
                event_id,
                slot_index,
            )
            if row is None or row["discord_user_id"] is not None:
                # Row missing (should not happen after INSERT above) or slot already taken
                return False
            # Step 3: claim the slot within the same transaction
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
) -> None:
    """Release a previously claimed slot lock — sets discord_user_id back to NULL.

    Called when:
      - The Notion booking write fails after a successful acquire_slot_lock()
      - A DJ self-cancels (Story 4.4)

    Idempotent: safe to call even if the row does not exist or is already NULL.

    [Source: epics.md#Story 4.2 AC3 (rollback on Notion failure)]
    [Source: epics.md#Story 4.4 (cancellation flow)]
    """
    await pool.execute(
        """
        UPDATE slot_locks
        SET discord_user_id = NULL, booked_at = NULL
        WHERE event_id = $1 AND slot_index = $2
        """,
        event_id,
        slot_index,
    )


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
        "SELECT event_id, slot_index FROM slot_locks WHERE discord_user_id = $1",
        discord_user_id,
    )
    return [{"event_id": row["event_id"], "slot_index": row["slot_index"]} for row in rows]
