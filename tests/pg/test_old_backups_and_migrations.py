"""Old backups are importable and migrated; migrations keep the data (real PostgreSQL).

Every test here builds the database at an older revision with the real migrations,
fills it, exports it (that is an "old backup"), brings the schema to the current
revision and imports the backup. A new migration is covered by these tests as soon as
it exists: the revision list comes from the migration files.
"""
import datetime
import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from bot.backup import restore
from bot.backup.export import export_database
from bot.db import pool as db_pool
from bot.ui import texts
from tests.pg import URL, run_alembic

REVISIONS = db_pool.revision_order()
HEAD = REVISIONS[-1]
OLD = REVISIONS[:-1]  # every revision that is not the current one
EVE = 4242
EVENTS_FROM = "0019"  # first revision whose schema the event helper writes


def _ctx(pool, **extra):  # noqa: ANN001, ANN202
    return SimpleNamespace(
        db=pool, maintenance=None, sync_problem=None, notion_client=None, sync_cycle=None,
        admins=SimpleNamespace(load=AsyncMock()),
        config=SimpleNamespace(postgres_url=URL), **extra,
    )


async def _pool():  # noqa: ANN202
    return await asyncpg.create_pool(URL, min_size=1, max_size=4)


@pytest.fixture(autouse=True)
def _leave_the_schema_at_head(schema):  # noqa: ANN001, ANN202
    """These tests move the schema around; the other tests of tests/pg expect head."""
    yield
    run_alembic("upgrade", "head")


async def _seed(pool, revision: str) -> None:  # noqa: ANN001
    """Add data that exists at `revision`; the migrations carry it up from there."""
    if revision == REVISIONS[0]:
        await pool.execute(
            "INSERT INTO djs (discord_user_id, dj_name) VALUES ($1, 'Eve')", EVE
        )
        await pool.execute("INSERT INTO config (guild_id, key, value) VALUES (1, 'k', 'v')")
    if revision == EVENTS_FROM:
        from bot.db.events import EventDraft, insert_event

        await insert_event(
            pool,
            EventDraft("E1", datetime.date(2030, 1, 2), datetime.time(20, 0), 60, 3),
            uid="u1",
        )


async def _backups_from_every_revision() -> dict[str, bytes]:
    """Walk the schema up one revision at a time with data in it and export at each stop.

    Each file is what a bot running that revision would have written; its data went
    through every earlier migration, like the data of a real installation.
    """
    run_alembic("downgrade", "base")
    backups: dict[str, bytes] = {}
    for revision in REVISIONS:
        run_alembic("upgrade", revision)
        pool = await _pool()
        try:
            await _seed(pool, revision)
            backups[revision] = await export_database(pool)
        finally:
            await pool.close()
        assert json.loads(gzip.decompress(backups[revision]))["alembic_revision"] == revision
    return backups


async def _backup_of(revision: str) -> bytes:
    """A backup made at `revision` (database built straight up to it, all data added there)."""
    run_alembic("downgrade", "base")
    run_alembic("upgrade", revision)
    pool = await _pool()
    try:
        await _seed(pool, REVISIONS[0])
        if REVISIONS.index(revision) >= REVISIONS.index(EVENTS_FROM):
            await _seed(pool, EVENTS_FROM)
        return await export_database(pool)
    finally:
        await pool.close()


def _expected_events(revision: str) -> int:
    return 1 if REVISIONS.index(revision) >= REVISIONS.index(EVENTS_FROM) else 0


async def _head_with_other_data() -> None:
    """Current schema holding data that is not in any backup."""
    run_alembic("upgrade", "head")
    pool = await _pool()
    try:
        await pool.execute("DELETE FROM events")
        await pool.execute("DELETE FROM djs")
        await pool.execute("DELETE FROM config")
        await pool.execute("INSERT INTO djs (discord_user_id, dj_name) VALUES (7, 'Other')")
    finally:
        await pool.close()


async def _state(pool) -> dict:  # noqa: ANN001
    return {
        "revision": await pool.fetchval("SELECT version_num FROM alembic_version"),
        "djs": [r["dj_name"] for r in await pool.fetch("SELECT dj_name FROM djs ORDER BY 1")],
    }


# --- migrations keep the data --------------------------------------------------------------


async def test_data_survives_every_migration_up_to_head() -> None:
    """A DJ, a setting and an event added at the first revision that has them are all
    still there (and valid for the current code) after the schema reaches head."""
    await _backups_from_every_revision()
    pool = await _pool()
    try:
        assert await _state(pool) == {"revision": HEAD, "djs": ["Eve"]}
        assert await pool.fetchval("SELECT value FROM config WHERE key = 'k'") == "v"
        assert await pool.fetchval("SELECT count(*) FROM events") == 1
    finally:
        await pool.close()


def test_every_migration_goes_down_and_up_on_its_own(schema) -> None:  # noqa: ANN001
    run_alembic("downgrade", "base")
    for revision in REVISIONS:
        run_alembic("upgrade", revision)
        run_alembic("downgrade", "-1")
        run_alembic("upgrade", "+1")
    run_alembic("upgrade", "head")


# --- old backups are imported and migrated ---------------------------------------------------


async def test_every_old_backup_is_imported_and_migrated(  # noqa: ANN001
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    backups = await _backups_from_every_revision()
    problems: list[str] = []
    for revision in OLD:
        await _head_with_other_data()
        pool = await _pool()
        try:
            ctx = _ctx(pool)
            result = await restore.import_database(ctx, backups[revision])
            if not result.ok or result.message != texts.IMPORT_DONE_MIGRATED:
                problems.append(f"{revision}: {result.message}")
                continue
            state = await _state(pool)
            events = await pool.fetchval("SELECT count(*) FROM events")
            config = await pool.fetchval("SELECT value FROM config WHERE key = 'k'")
            if state != {"revision": HEAD, "djs": ["Eve"]} or config != "v":
                problems.append(f"{revision}: wrong data after the import: {state}, {config}")
            if events != _expected_events(revision):
                problems.append(f"{revision}: {events} events after the import")
            if ctx.maintenance is not None:
                problems.append(f"{revision}: the bot stays paused")
            # The migrated database is a normal one: it exports and imports at head.
            again = await restore.import_database(ctx, await export_database(pool))
            if not again.ok or again.message != texts.IMPORT_DONE:
                problems.append(f"{revision}: the migrated data cannot be exported again")
        finally:
            await pool.close()
    assert problems == []
    assert list(tmp_path.glob("*before-import*"))  # safety exports of the replaced data


async def test_a_backup_of_the_current_revision_still_imports_directly(  # noqa: ANN001
    schema, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    backup = await _backup_of(HEAD)
    await _head_with_other_data()
    pool = await _pool()
    try:
        result = await restore.import_database(_ctx(pool), backup)
        assert result.ok and result.message == texts.IMPORT_DONE
        assert (await _state(pool))["djs"] == ["Eve"]
    finally:
        await pool.close()


# --- failures leave a usable database --------------------------------------------------------


def _failing(monkeypatch, *, fail):  # noqa: ANN001, ANN202
    """Make run_alembic fail for the calls `fail(args, number)` selects (all others run)."""
    real = db_pool.run_alembic
    calls: list[tuple] = []

    async def fake(url, *args):  # noqa: ANN001, ANN202
        calls.append(args)
        if fail(args, len(calls)):
            return False, "boom"
        return await real(url, *args)

    monkeypatch.setattr(db_pool, "run_alembic", fake)
    return calls


async def test_a_failed_migration_restores_the_current_data(  # noqa: ANN001
    schema, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    backup = await _backup_of(OLD[-1])
    await _head_with_other_data()
    # Only the first way up fails; the way up for the recovery works.
    _failing(monkeypatch, fail=lambda args, n: args == ("upgrade", "head") and n == 2)
    pool = await _pool()
    try:
        ctx = _ctx(pool)
        result = await restore.import_database(ctx, backup)
        assert not result.ok and result.message == texts.IMPORT_MIGRATE_FAILED
        assert await _state(pool) == {"revision": HEAD, "djs": ["Other"]}
        assert ctx.maintenance is None
    finally:
        await pool.close()


async def test_a_migration_that_cannot_be_undone_keeps_the_bot_paused(  # noqa: ANN001
    schema, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    backup = await _backup_of(OLD[-1])
    await _head_with_other_data()
    _failing(monkeypatch, fail=lambda args, n: args == ("upgrade", "head"))
    pool = await _pool()
    try:
        ctx = _ctx(pool)
        result = await restore.import_database(ctx, backup)
        assert not result.ok and result.message == texts.IMPORT_MIGRATE_STUCK
        assert ctx.maintenance == texts.MAINTENANCE  # bookings stay paused
    finally:
        await pool.close()
    run_alembic("upgrade", "head")  # what a restart does
    pool = await _pool()
    try:
        assert (await _state(pool))["revision"] == HEAD
    finally:
        await pool.close()


async def test_a_failed_downgrade_changes_nothing(  # noqa: ANN001
    schema, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    backup = await _backup_of(OLD[-1])
    await _head_with_other_data()
    _failing(monkeypatch, fail=lambda args, n: args[0] == "downgrade")
    pool = await _pool()
    try:
        ctx = _ctx(pool)
        result = await restore.import_database(ctx, backup)
        assert not result.ok and result.message == texts.IMPORT_MIGRATE_FAILED
        assert await _state(pool) == {"revision": HEAD, "djs": ["Other"]}
        assert ctx.maintenance is None
    finally:
        await pool.close()


async def test_a_backup_from_a_newer_or_unknown_revision_is_refused(  # noqa: ANN001
    schema, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    await _head_with_other_data()
    pool = await _pool()
    try:
        document = json.loads(gzip.decompress(await export_database(pool)))
        document["alembic_revision"] = "9999_future"
        result = await restore.import_database(
            _ctx(pool), gzip.compress(json.dumps(document).encode())
        )
        assert not result.ok and "9999_future" in result.message
        assert (await _state(pool))["djs"] == ["Other"]
    finally:
        await pool.close()
