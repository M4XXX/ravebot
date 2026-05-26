"""Interventions repository -- Story 6.2.

Tracks which DJs are currently under admin intervention.
A row in the interventions table means the DJ is under intervention;
absence of a row means normal automated operation.

INSERT ... ON CONFLICT DO NOTHING makes start_intervention idempotent.

[Source: architecture.md#Repository Pattern]
[Source: epics.md#Story 6.2 -- FR32, FR33]
[Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
"""
import asyncpg


async def is_under_intervention(
    pool: asyncpg.Pool,
    discord_user_id: int,
) -> bool:
    """Return True if this DJ is currently under admin intervention.

    A row in the interventions table means intervention is active.
    [Source: epics.md#Story 6.2 AC3 -- suppress automated response during intervention]
    """
    row = await pool.fetchrow(
        "SELECT 1 FROM interventions WHERE discord_user_id = $1",
        discord_user_id,
    )
    return row is not None


async def start_intervention(
    pool: asyncpg.Pool,
    discord_user_id: int,
) -> None:
    """Insert an intervention record for this DJ.

    ON CONFLICT DO NOTHING makes this idempotent -- calling twice is safe.
    [Source: epics.md#Story 6.2 AC1 -- pauses all automated messages to the DJ]
    """
    await pool.execute(
        "INSERT INTO interventions (discord_user_id) VALUES ($1) ON CONFLICT DO NOTHING",
        discord_user_id,
    )


async def end_intervention(
    pool: asyncpg.Pool,
    discord_user_id: int,
) -> None:
    """Remove the intervention record for this DJ, resuming normal operation.

    No-op if the DJ was not under intervention.
    [Source: epics.md#Story 6.2 AC4 -- resumes normal automated behaviour]
    """
    await pool.execute(
        "DELETE FROM interventions WHERE discord_user_id = $1",
        discord_user_id,
    )
