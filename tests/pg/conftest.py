"""Fixtures for the opt-in real-SQL tests (see test_postgres.py for how to run them)."""
import pytest

from tests.pg import URL, run_alembic


@pytest.fixture(scope="module", autouse=True)
def schema():
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")
    yield
    run_alembic("downgrade", "base")


@pytest.fixture
async def pool():
    import asyncpg

    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE slot_locks, djs, config RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()
