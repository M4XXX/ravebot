"""Migration 0018 against real Postgres: the columns go, a downgrade restores page ids."""
from tests.pg import URL, run_alembic

A = 111


async def _columns(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'djs'"
    )
    return {r["column_name"] for r in rows}


async def _has_table(conn, name: str) -> bool:
    return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", name)


async def test_0018_drops_columns_and_downgrade_restores_page_ids(pool) -> None:
    async with pool.acquire() as conn:
        assert "notion_dj_page_id" not in await _columns(conn)
        assert not await _has_table(conn, "event_shapes")
        await conn.execute(
            "INSERT INTO djs (discord_user_id, dj_name, onboarding_complete, onboarding_step)"
            " VALUES ($1, 'Nova', true, 0)", A,
        )
        await conn.execute(
            "INSERT INTO notion_mirror (entity, entity_key, notion_page_id)"
            " VALUES ('dj', $1, 'page-1')", str(A),
        )
    await pool.close()
    try:
        run_alembic("downgrade", "0017")
        import asyncpg

        conn = await asyncpg.connect(URL)
        try:
            assert "notion_dj_page_id" in await _columns(conn)
            assert await _has_table(conn, "event_shapes")
            assert await conn.fetchval(
                "SELECT notion_dj_page_id FROM djs WHERE discord_user_id = $1", A
            ) == "page-1"
            assert await conn.fetchval("SELECT count(*) FROM event_shapes") == 0
        finally:
            await conn.close()
    finally:
        run_alembic("upgrade", "head")
