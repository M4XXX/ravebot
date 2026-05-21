"""Reminders DB repository — day-before reminder deduplication.

Handles: FR29 — prevent duplicate reminders per (event, DJ, slot).

[Source: architecture.md#Repository Pattern]
[Source: epics.md#Story 5.4 AC2, AC4]
"""
import asyncpg


async def has_reminder_been_sent(
    pool: asyncpg.Pool,
    event_id: str,
    discord_user_id: int,
    slot_index: int,
) -> bool:
    """Return True if a reminder has already been sent for this (event, DJ, slot).

    Uses SELECT EXISTS for an O(1) index scan on the composite PK.
    Returns False if no row exists (reminder not yet sent).

    [Source: epics.md#Story 5.4 AC2 — prevent duplicates]
    [Source: epics.md#Story 5.4 AC4 — no second reminder after cancellation]
    """
    result = await pool.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM reminders_sent
            WHERE event_id = $1 AND discord_user_id = $2 AND slot_index = $3
        )
        """,
        event_id,
        discord_user_id,
        slot_index,
    )
    return bool(result)


async def record_reminder_sent(
    pool: asyncpg.Pool,
    event_id: str,
    discord_user_id: int,
    slot_index: int,
) -> None:
    """Record that a reminder DM was successfully sent.

    ON CONFLICT DO NOTHING makes this idempotent — safe to call even if
    a row already exists (e.g., concurrent task cycles).

    Only call this AFTER a successful user.send() — do NOT record for failed
    DM attempts (caller retries on next cycle within the 20-28h window).

    [Source: epics.md#Story 5.4 AC2]
    [Source: architecture.md#Repository Pattern]
    """
    await pool.execute(
        """
        INSERT INTO reminders_sent (event_id, discord_user_id, slot_index)
        VALUES ($1, $2, $3)
        ON CONFLICT (event_id, discord_user_id, slot_index) DO NOTHING
        """,
        event_id,
        discord_user_id,
        slot_index,
    )
