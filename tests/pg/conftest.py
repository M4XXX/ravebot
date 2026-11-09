"""Fixtures for the opt-in real-SQL tests (see tests/pg/__init__.py for how to run them)."""
import pytest

from tests.pg import URL, run_alembic

TABLES = (
    "slot_locks, reminders_sent, outreach_contacts, outreach_sequences, dj_conversations, "
    "djs, config, web_admins, fully_booked_events, log_replies, setup_drafts, "
    "event_shapes, manual_events"
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests/pg/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.pg)
            if not URL:
                item.add_marker(pytest.mark.skip(reason="RAVEBOT_TEST_POSTGRES_URL not set"))


@pytest.fixture(scope="session")
def schema():
    if not URL:
        pytest.skip("RAVEBOT_TEST_POSTGRES_URL not set")
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")
    yield
    run_alembic("downgrade", "base")


@pytest.fixture
async def pool(schema):
    import asyncpg

    pool = await asyncpg.create_pool(URL, min_size=1, max_size=6)
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        await conn.execute("ALTER SEQUENCE guest_dj_id_seq RESTART")
    yield pool
    await pool.close()
