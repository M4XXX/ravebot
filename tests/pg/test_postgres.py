"""Real-SQL tests against a disposable PostgreSQL (opt-in).

Everything else in the suite mocks asyncpg, so no SQL statement and no migration ever ran
under test. These tests run only when RAVEBOT_TEST_POSTGRES_URL points at an EMPTY,
throw-away database, e.g.

    docker run --rm -d -p 127.0.0.1:55432:5432 -e POSTGRES_USER=ravebot \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=ravebot_test postgres:16-alpine
    RAVEBOT_TEST_POSTGRES_URL=postgresql://ravebot:test@127.0.0.1:55432/ravebot_test \\
        uv run pytest -m pg

The schema is dropped and recreated with alembic; never point this at real data.
"""
import asyncio
import os
import subprocess
import sys

import pytest

URL = os.environ.get("RAVEBOT_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not URL, reason="RAVEBOT_TEST_POSTGRES_URL not set (opt-in real-SQL tests)"),
]


def _alembic(*args: str) -> None:
    env = {**os.environ, "POSTGRES_URL": URL or ""}
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env,
                   capture_output=True, text=True)


@pytest.fixture(scope="module", autouse=True)
def schema():
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    yield
    _alembic("downgrade", "base")


@pytest.fixture
async def pool():
    import asyncpg

    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE slot_locks, djs, config RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()


def test_migrations_go_down_and_up_again() -> None:
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")


async def test_two_djs_racing_for_one_slot_get_exactly_one_lock(pool) -> None:
    from bot.db.bookings import acquire_slot_lock, init_event_slots

    await init_event_slots(pool, "evt", 6)
    results = await asyncio.gather(
        acquire_slot_lock(pool, "evt", 2, 111),
        acquire_slot_lock(pool, "evt", 2, 222),
    )
    assert sorted(results) == [False, True]


async def test_booked_slots_query_and_release(pool) -> None:
    from bot.db.bookings import (
        acquire_slot_lock,
        get_booked_slots,
        init_event_slots,
        release_slot_lock,
    )

    await init_event_slots(pool, "a", 6)
    await init_event_slots(pool, "b", 6)
    assert await acquire_slot_lock(pool, "a", 3, 111)
    assert await acquire_slot_lock(pool, "b", 0, 222)
    assert await get_booked_slots(pool, ["a", "b", "c"]) == {"a": {3: 111}, "b": {0: 222}}
    await release_slot_lock(pool, "a", 3)
    assert await get_booked_slots(pool, ["a"]) == {}


async def test_profile_resubmission_updates_instead_of_failing(pool) -> None:
    from bot.db.djs import (
        get_dj_by_discord_id,
        get_notion_page_ids,
        insert_dj,
        set_notion_dj_page_id,
    )

    for name in ("First Name", "Second Name"):
        await insert_dj(pool, discord_user_id=7, dj_name=name, vrchat_name=None, logo_url=None,
                        stream_url_primary="https://example.com/a", stream_url_backup=None)
    row = await get_dj_by_discord_id(pool, 7)
    assert row["dj_name"] == "Second Name" and row["onboarding_complete"] is True
    await set_notion_dj_page_id(pool, 7, "page-7")
    assert await get_notion_page_ids(pool, [7, 8]) == {7: "page-7"}


@pytest.mark.passive  # use the real bot.active, not the conftest stub
async def test_config_round_trip(pool) -> None:
    from bot import active
    from bot.db.config import get_value, set_value

    assert await active.is_active(pool, 42) is False
    await active.set_active(pool, 42, True)
    assert await active.is_active(pool, 42) is True
    await set_value(pool, 42, "timezone", "Europe/Berlin")
    assert await get_value(pool, 42, "timezone") == "Europe/Berlin"
