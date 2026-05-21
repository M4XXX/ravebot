"""Outreach sequences and contacts repository.

Tables:
  outreach_sequences — one row per Notion event_id; tracks sequence lifecycle
  outreach_contacts  — one row per (event_id, discord_user_id); tracks per-DJ contact history

outreach_sequences status lifecycle:
  active     — sequence started; gap detected, DJ outreach in progress (Story 5.2)
  complete   — all slots filled; no further outreach needed (Story 5.1 AC4)
  exhausted  — all eligible DJs contacted, slots still open (Story 5.3 alert)

outreach_contacts status lifecycle:
  sent        — DM sent, awaiting response
  accepted    — DJ tapped [Accept — Show Slots]
  declined    — DJ tapped [Not this time]
  timeout     — 12-hour OutreachView timeout fired; no response
  unreachable — DM delivery failed or user not in bot cache

[Source: epics.md#Story 5.1 AC2, AC3, AC4]
[Source: epics.md#Story 5.2 AC1-AC5]
[Source: architecture.md#Repository Pattern]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
"""
import asyncpg


async def get_sequence(pool: asyncpg.Pool, event_id: str) -> dict | None:
    """Fetch the outreach sequence row for this event, or None if not found.

    [Source: epics.md#Story 5.1 AC2, AC3]
    """
    row = await pool.fetchrow(
        "SELECT event_id, status, last_contact_at, created_at"
        " FROM outreach_sequences WHERE event_id = $1",
        event_id,
    )
    if row is None:
        return None
    return dict(row)


async def upsert_sequence(pool: asyncpg.Pool, event_id: str) -> None:
    """Insert a new outreach sequence row. No-op if already exists (idempotent).

    Called by OutreachService.run_gap_detection() when an event with open slots
    is first detected within the outreach window.

    [Source: epics.md#Story 5.1 AC2]
    """
    await pool.execute(
        """
        INSERT INTO outreach_sequences (event_id)
        VALUES ($1)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id,
    )


async def mark_sequence_complete(pool: asyncpg.Pool, event_id: str) -> None:
    """Mark outreach sequence as complete — all slots for this event are filled.

    Called by OutreachService.run_gap_detection() when open_slots <= 0 and
    an active sequence exists.

    [Source: epics.md#Story 5.1 AC4]
    """
    await pool.execute(
        "UPDATE outreach_sequences SET status = 'complete' WHERE event_id = $1",
        event_id,
    )


async def mark_sequence_exhausted(pool: asyncpg.Pool, event_id: str) -> None:
    """Mark outreach sequence as exhausted — all eligible DJs contacted, slots remain open.

    Called by Story 5.3 (OutreachExhaustionAlert) when no more eligible DJs remain.

    [Source: epics.md#Story 5.3 AC1]
    """
    await pool.execute(
        "UPDATE outreach_sequences SET status = 'exhausted' WHERE event_id = $1",
        event_id,
    )


async def reactivate_sequence(pool: asyncpg.Pool, event_id: str) -> None:
    """Reset an exhausted outreach sequence back to active.

    Called by OutreachService.run_gap_detection() when a new Active Booking DJ
    is detected who has not been contacted in this sequence. Allows a fresh
    outreach pass without losing the existing outreach_contacts history.

    [Source: epics.md#Story 5.3 AC3]
    """
    await pool.execute(
        "UPDATE outreach_sequences SET status = 'active' WHERE event_id = $1",
        event_id,
    )


async def update_last_contact_at(pool: asyncpg.Pool, event_id: str) -> None:
    """Set last_contact_at to NOW() — records when the most recent DJ DM was sent.

    Called by Story 5.2 (OutreachService.contact_next_dj) after each outreach DM
    is successfully dispatched. The cooldown check in run_gap_detection() compares
    NOW() against this value.

    [Source: epics.md#Story 5.1 AC3]
    [Source: epics.md#Story 5.2]
    """
    await pool.execute(
        "UPDATE outreach_sequences SET last_contact_at = NOW() WHERE event_id = $1",
        event_id,
    )


# ---------------------------------------------------------------------------
# outreach_contacts — Story 5.2
# ---------------------------------------------------------------------------


async def get_contacted_discord_ids(pool: asyncpg.Pool, event_id: str) -> list[int]:
    """Return discord_user_ids already contacted in this outreach sequence.

    Used by OutreachService.contact_next_dj() to exclude already-contacted DJs
    from the eligible pool.

    [Source: epics.md#Story 5.2 AC1]
    """
    rows = await pool.fetch(
        "SELECT discord_user_id FROM outreach_contacts WHERE event_id = $1",
        event_id,
    )
    return [row["discord_user_id"] for row in rows]


async def record_outreach_contact(
    pool: asyncpg.Pool, event_id: str, discord_user_id: int
) -> None:
    """Record a DJ contact attempt for this outreach sequence (idempotent).

    Called by OutreachService.contact_next_dj() BEFORE sending the DM so the
    contact is recorded even if the send fails.

    ON CONFLICT DO NOTHING ensures idempotency across bot restarts.

    [Source: epics.md#Story 5.2 AC1, AC4]
    """
    await pool.execute(
        """
        INSERT INTO outreach_contacts (event_id, discord_user_id)
        VALUES ($1, $2)
        ON CONFLICT (event_id, discord_user_id) DO NOTHING
        """,
        event_id,
        discord_user_id,
    )


async def update_outreach_contact_status(
    pool: asyncpg.Pool, event_id: str, discord_user_id: int, status: str
) -> None:
    """Update the status of an outreach contact row.

    Called after send outcome is known:
      - 'accepted'    — DJ tapped [Accept — Show Slots]
      - 'declined'    — DJ tapped [Not this time]
      - 'timeout'     — OutreachView.on_timeout() fired
      - 'unreachable' — disnake.Forbidden or user not in bot cache

    [Source: epics.md#Story 5.2 AC2-AC5]
    """
    await pool.execute(
        "UPDATE outreach_contacts SET status = $1"
        " WHERE event_id = $2 AND discord_user_id = $3",
        status,
        event_id,
        discord_user_id,
    )
