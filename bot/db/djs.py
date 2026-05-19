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
    """Insert a new DJ profile row with onboarding completed.

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
        """,
        discord_user_id,
        dj_name,
        vrchat_name,
        logo_url,
        stream_url_primary,
        stream_url_backup,
    )
