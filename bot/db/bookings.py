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
                SET discord_user_id = $3, booked_at = NOW(), plan_text = NULL
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
    discord_user_id: int | None = None,
) -> bool:
    """Release a previously claimed slot lock — sets discord_user_id back to NULL.

    Called when:
      - The Notion booking write fails after a successful acquire_slot_lock()
      - A DJ self-cancels (Story 4.4)

    With discord_user_id only that DJ's own booking is freed: a stale cancel button or
    a rollback can never free a slot another DJ booked in the meantime. Without it the
    slot is freed whoever holds it (admin cancel).

    Idempotent: safe to call even if the row does not exist or is already NULL.
    Returns True if a booking was actually freed.

    [Source: epics.md#Story 4.2 AC3 (rollback on Notion failure)]
    [Source: epics.md#Story 4.4 (cancellation flow)]
    """
    status = await pool.execute(
        """
        UPDATE slot_locks
        SET discord_user_id = NULL, booked_at = NULL, plan_text = NULL
        WHERE event_id = $1 AND slot_index = $2
          AND discord_user_id IS NOT NULL
          AND ($3::bigint IS NULL OR discord_user_id = $3)
        """,
        event_id,
        slot_index,
        discord_user_id,
    )
    return status != "UPDATE 0"


async def holds_slot_in_event(
    pool: asyncpg.Pool,
    event_id: str,
    discord_user_id: int,
) -> bool:
    """True if this DJ still holds any slot of the event.

    The Notion DJs relation has one entry per DJ, not per slot, so a DJ is only
    unlinked from the event once this returns False.
    """
    return bool(
        await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM slot_locks WHERE event_id = $1 AND discord_user_id = $2)",
            event_id,
            discord_user_id,
        )
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


async def admin_force_book_slot(
    pool: asyncpg.Pool,
    event_id: str,
    slot_index: int,
    discord_user_id: int,
) -> None:
    """Force-book a slot for a DJ, overwriting any existing holder (admin override only).

    Uses an UPSERT — does NOT use SELECT...FOR UPDATE. This intentionally bypasses
    the race condition guard used in acquire_slot_lock(). Only callable from
    admin slash commands where the admin has already decided to overwrite.

    [Source: epics.md#Story 6.3 AC1]
    [Source: architecture.md#Repository Pattern]
    """
    await pool.execute(
        """
        INSERT INTO slot_locks (event_id, slot_index, discord_user_id, booked_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (event_id, slot_index) DO UPDATE
            SET discord_user_id = $3, booked_at = NOW(), plan_text = NULL
        """,
        event_id,
        slot_index,
        discord_user_id,
    )


async def hand_back_slot(
    pool: asyncpg.Pool,
    event_id: str,
    slot_index: int,
    from_user_id: int,
    to_user_id: int,
    plan_text: str | None = None,
) -> bool:
    """Give a slot back from one DJ to another, only if the first still holds it.

    Undoes an admin force-book whose Notion write failed. Between the force-book and the
    undo the slot may have been cancelled and booked by someone else; that booking stays.
    Returns True if the slot was handed back.
    """
    status = await pool.execute(
        """
        UPDATE slot_locks
        SET discord_user_id = $4, booked_at = NOW(), plan_text = $5
        WHERE event_id = $1 AND slot_index = $2 AND discord_user_id = $3
        """,
        event_id,
        slot_index,
        from_user_id,
        to_user_id,
        plan_text,
    )
    return status != "UPDATE 0"


async def init_event_slots(
    pool: asyncpg.Pool,
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


async def get_booked_slots(pool: asyncpg.Pool, event_ids: list[str]) -> dict[str, dict[int, int]]:
    """Booked slot indices per event in one query: {event_id: {slot_index: discord_user_id}}.

    Events without bookings are absent from the result.
    """
    if not event_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT event_id, slot_index, discord_user_id
        FROM slot_locks
        WHERE event_id = ANY($1::text[]) AND discord_user_id IS NOT NULL
        """,
        event_ids,
    )
    result: dict[str, dict[int, int]] = {}
    for row in rows:
        result.setdefault(row["event_id"], {})[row["slot_index"]] = row["discord_user_id"]
    return result


async def set_plan_text(
    pool: asyncpg.Pool, event_id: str, slot_index: int, discord_user_id: int, text: str
) -> None:
    """Record the text the bot wrote into the slot's plan column (tds layout).

    Only while the slot is still this DJ's: a booking undone meanwhile records nothing.
    """
    await pool.execute(
        """
        UPDATE slot_locks SET plan_text = $4
        WHERE event_id = $1 AND slot_index = $2 AND discord_user_id = $3
        """,
        event_id,
        slot_index,
        discord_user_id,
        text,
    )


async def get_plan_text(pool: asyncpg.Pool, event_id: str, slot_index: int) -> str | None:
    """The text the bot wrote into the slot's plan column, or None if it wrote none."""
    return await pool.fetchval(
        "SELECT plan_text FROM slot_locks WHERE event_id = $1 AND slot_index = $2",
        event_id,
        slot_index,
    )


async def get_plan_texts(
    pool: asyncpg.Pool, event_ids: list[str]
) -> dict[tuple[str, int], str]:
    """Recorded plan texts of booked slots: {(event_id, slot_index): text}."""
    if not event_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT event_id, slot_index, plan_text FROM slot_locks
        WHERE event_id = ANY($1::text[]) AND discord_user_id IS NOT NULL
          AND plan_text IS NOT NULL
        """,
        event_ids,
    )
    return {(row["event_id"], row["slot_index"]): row["plan_text"] for row in rows}
