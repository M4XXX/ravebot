"""DJ repository — implemented across Story 2.x.

Handles: FR2, FR6, FR7, FR42 — DJ CRUD.
All queries are ownership-scoped via WHERE discord_user_id = $1.

[Source: architecture.md#Repository Pattern]
[Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
"""
import asyncpg

from bot.services.dj_validation import clean_text


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
    logo_image: bytes | None = None,
) -> None:
    """Insert a new DJ profile row with onboarding completed.

    Sets active_booking=true, onboarding_complete=true, onboarding_step=0.
    Called when the DJ confirms the onboarding conversation.
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
            logo_image, logo_synced,
            active_booking, onboarding_complete, onboarding_step
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, true, 0)
        """,
        discord_user_id,
        dj_name,
        vrchat_name,
        logo_url,
        stream_url_primary,
        stream_url_backup,
        logo_image,
        logo_image is None,
    )


async def get_notion_dj_page_id(
    pool: asyncpg.Pool,
    discord_user_id: int,
) -> str | None:
    """Return the stored Notion page ID for this DJ, or None if not set.

    Used to look up the DJ's row in the Notion DJs database (see ensure_dj_page).

    None is returned for DJs without a Notion row yet (migration 0007 cleared the
    IDs of the old Notion database). ensure_dj_page creates the row on demand.

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

    Called by the DJ conversation service after NotionClient.upsert_dj() succeeds,
    and by ensure_dj_page(). The ID points to a row in the Notion DJs database.

    Ownership-scoped: WHERE discord_user_id = $2.
    [Source: epics.md#Story 4.2 AC2]
    [Source: architecture.md#DJ Data Isolation: Ownership-Scoped Queries]
    """
    await pool.execute(
        "UPDATE djs SET notion_dj_page_id = $1 WHERE discord_user_id = $2",
        notion_page_id,
        discord_user_id,
    )


async def clear_all_notion_dj_page_ids(pool: asyncpg.Pool) -> None:
    """Clear every stored Notion DJ page id (all DJs).

    Called by ensure_schema when it creates a fresh set of Notion databases: the
    stored ids point into the abandoned DJs database. ensure_dj_page recreates
    the rows on demand.
    """
    await pool.execute("UPDATE djs SET notion_dj_page_id = NULL")


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

    Used by AdminCog /list all djs to show the full DJ pool with active status.
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


_UPDATABLE_FIELDS = frozenset(
    {
        "dj_name",
        "vrchat_name",
        "stream_url_primary",
        "stream_url_backup",
        "logo_image",
        "logo_url",
        "logo_synced",
    }
)


async def update_dj_fields(pool: asyncpg.Pool, user_id: int, **fields: object) -> bool:
    """Update allowlisted djs columns. Returns True if the row was updated.

    Changing logo_image also clears logo_url and marks the logo unsynced unless
    the caller passes logo_synced explicitly. Unknown keys raise ValueError.
    """
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown dj fields: {sorted(unknown)}")
    if not fields:
        return False
    assignments: list[str] = []
    params: list[object] = []
    for name, value in fields.items():
        params.append(value)
        assignments.append(f"{name} = ${len(params)}")
    if "logo_image" in fields:
        if "logo_url" not in fields:
            assignments.append("logo_url = NULL")
        if "logo_synced" not in fields:
            assignments.append("logo_synced = false")
    params.append(user_id)
    result = await pool.execute(
        f"UPDATE djs SET {', '.join(assignments)} WHERE discord_user_id = ${len(params)}",
        *params,
    )
    return result == "UPDATE 1"


async def dj_name_exists(
    pool: asyncpg.Pool, name: str, exclude_user_id: int | None = None
) -> bool:
    """True if another DJ uses this name (trimmed, whitespace collapsed, case-insensitive)."""
    found = await pool.fetchval(
        r"""
        SELECT 1 FROM djs
        WHERE lower(regexp_replace(btrim(dj_name), '\s+', ' ', 'g')) = $1
          AND ($2::bigint IS NULL OR discord_user_id <> $2)
        LIMIT 1
        """,
        clean_text(name).casefold(),
        exclude_user_id,
    )
    return found is not None


async def get_logo_image(pool: asyncpg.Pool, user_id: int) -> bytes | None:
    return await pool.fetchval(
        "SELECT logo_image FROM djs WHERE discord_user_id = $1", user_id
    )


async def set_logo_synced(pool: asyncpg.Pool, user_id: int, synced: bool) -> None:
    await pool.execute(
        "UPDATE djs SET logo_synced = $1 WHERE discord_user_id = $2", synced, user_id
    )


async def list_unsynced_logo_user_ids(pool: asyncpg.Pool) -> list[int]:
    rows = await pool.fetch(
        "SELECT discord_user_id FROM djs WHERE logo_synced = false"
    )
    return [row["discord_user_id"] for row in rows]
