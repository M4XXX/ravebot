"""DJ repository — implemented across Story 2.x.

Handles: FR2, FR6, FR7, FR42 — DJ CRUD.
All queries are ownership-scoped via WHERE discord_user_id = $1.

[Source: architecture.md#Repository Pattern]
[Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
"""
import asyncpg


async def get_dj_by_discord_id(
    pool: asyncpg.Pool,
    discord_user_id: int,
) -> asyncpg.Record | None:
    """Return the djs row for this Discord user, or None if not found.

    Ownership-scoped: only returns data for the requesting user's ID.
    Used in on_member_update to check for duplicate onboarding triggers.

    [Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
    [Source: epics.md#Story 2.1 AC1, AC4]
    """
    return await pool.fetchrow(
        "SELECT * FROM djs WHERE discord_user_id = $1",
        discord_user_id,
    )


async def insert_dj(
    pool: asyncpg.Pool,
    *,
    discord_user_id: int,
    dj_name: str,
    vrchat_name: str | None,
    logo_url: str | None,
    stream_url_primary: str | None,
    stream_url_backup: str | None,
) -> None:
    """Insert or update a DJ profile row with onboarding completed.

    Re-submitting the form (e.g. via an old onboarding DM) updates the row instead of
    failing on the primary key.

    Sets active_booking=true, onboarding_complete=true, onboarding_step=0.
    Called on successful OnboardingModal submission (Story 2.2).
    Role assignment and Notion sync deferred to Story 2.3.

    Ownership-scoped: discord_user_id is the PK — insert is safe.

    [Source: architecture.md#Repository Pattern]
    [Source: epics.md#Story 2.2 AC2]
    [Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
    """
    await pool.execute(
        """
        INSERT INTO djs (
            discord_user_id, dj_name, vrchat_name, logo_url,
            stream_url_primary, stream_url_backup,
            active_booking, onboarding_complete, onboarding_step
        )
        VALUES ($1, $2, $3, $4, $5, $6, true, true, 0)
        ON CONFLICT (discord_user_id) DO UPDATE SET
            dj_name = EXCLUDED.dj_name,
            vrchat_name = EXCLUDED.vrchat_name,
            logo_url = EXCLUDED.logo_url,
            stream_url_primary = EXCLUDED.stream_url_primary,
            stream_url_backup = EXCLUDED.stream_url_backup,
            active_booking = true,
            onboarding_complete = true,
            onboarding_step = 0
        """,
        discord_user_id,
        dj_name,
        vrchat_name,
        logo_url,
        stream_url_primary,
        stream_url_backup,
    )


async def get_notion_dj_page_id(
    pool: asyncpg.Pool,
    discord_user_id: int,
) -> str | None:
    """Return the stored Notion page ID for this DJ, or None if not set.

    Used by BookingConfirmView to look up the DJ's Notion page ID before
    writing the booking to the Notion event's DJs relation.

    None is returned for DJs onboarded before Story 4.2 (notion_dj_page_id
    column was NULL at migration time). The booking flow handles None gracefully.

    Ownership-scoped: only returns data for the requesting user's ID.
    [Source: epics.md#Story 4.2 AC2]
    [Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
    """
    row = await pool.fetchrow(
        "SELECT notion_dj_page_id FROM djs WHERE discord_user_id = $1",
        discord_user_id,
    )
    if row is None:
        return None
    return row["notion_dj_page_id"]


async def set_notion_dj_page_id(
    pool: asyncpg.Pool,
    discord_user_id: int,
    notion_page_id: str,
) -> None:
    """Store the Notion page ID for this DJ after onboarding Notion sync.

    Called from OnboardingModal.callback() after write_dj_record() succeeds.
    Enables BookingConfirmView to later look up the DJ's Notion page ID.

    Ownership-scoped: WHERE discord_user_id = $2.
    [Source: epics.md#Story 4.2 AC2]
    [Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
    """
    await pool.execute(
        "UPDATE djs SET notion_dj_page_id = $1 WHERE discord_user_id = $2",
        notion_page_id,
        discord_user_id,
    )


async def get_active_booking_djs(pool: asyncpg.Pool) -> list[dict]:
    """Return all DJs with active_booking=true, sorted by last_contacted_at ASC NULLS FIRST.

    NULLS FIRST gives highest priority to DJs never previously contacted.
    Used by OutreachService.contact_next_dj() for fair cross-sequence DJ selection.

    [Source: epics.md#Story 5.2 AC1 — 'prioritising those not recently contacted
    across all sequences']
    """
    rows = await pool.fetch(
        """
        SELECT discord_user_id, dj_name, last_contacted_at
        FROM djs
        WHERE active_booking = true
        ORDER BY last_contacted_at ASC NULLS FIRST
        """,
    )
    return [dict(row) for row in rows]


async def update_last_contacted_at(pool: asyncpg.Pool, discord_user_id: int) -> None:
    """Record when a DJ was last contacted (globally, across all sequences).

    Called after a successful outreach DM send. Used for cross-sequence DJ
    prioritisation in get_active_booking_djs() ordering.

    [Source: epics.md#Story 5.2 AC1]
    """
    await pool.execute(
        "UPDATE djs SET last_contacted_at = NOW() WHERE discord_user_id = $1",
        discord_user_id,
    )


async def get_all_djs(pool: asyncpg.Pool) -> list[dict]:
    """Return all registered DJ rows ordered alphabetically by dj_name.

    Used by AdminCog /list-djs to show the full DJ pool with active status.
    Returns empty list if no DJs are registered.

    [Source: epics.md#Story 6.5 AC3, AC4 — FR39]
    [Source: architecture.md#Repository Pattern]
    """
    rows = await pool.fetch(
        "SELECT discord_user_id, dj_name, active_booking FROM djs ORDER BY dj_name ASC"
    )
    return [dict(row) for row in rows]


async def update_active_booking(
    pool: asyncpg.Pool,
    *,
    discord_user_id: int,
    active_booking: bool,
) -> bool:
    """Set active_booking status for a DJ. Returns True if the row was found and updated.

    Returns False if no DJ row exists (DJ never completed onboarding).
    Called by FallbackMenuView go_active and go_inactive handlers (Story 2.4).

    Ownership-scoped: WHERE discord_user_id = $2.
    [Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
    [Source: epics.md#Story 2.4 AC3, AC4]
    """
    result = await pool.execute(
        "UPDATE djs SET active_booking = $1 WHERE discord_user_id = $2",
        active_booking,
        discord_user_id,
    )
    return result == "UPDATE 1"
