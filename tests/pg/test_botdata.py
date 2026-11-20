"""Real-SQL checks of bot/sync/botdata.py."""
from bot.sync.botdata import botdata_rows, restore_botdata


async def test_botdata_excludes_admins_and_notion_ids(pool) -> None:
    await pool.execute("INSERT INTO web_admins (discord_user_id) VALUES (1)")
    await pool.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (0, 'notion_events_ds', 'x')"
    )
    await pool.execute("INSERT INTO config (guild_id, key, value) VALUES (7, 'log_channel', '9')")
    await pool.execute(
        "INSERT INTO dj_conversations (discord_user_id, guild_id, kind, step, logo_image)"
        " VALUES (3, 7, 'k', 's', '\\x0102')"
    )
    rows = await botdata_rows(pool)
    assert set(rows) == {"config:7:log_channel", "dj_conversations:3"}
    assert "logo_image" not in rows["dj_conversations:3"][1]
    assert not any(k.startswith("web_admins") for k in rows)


async def test_rebuild_restores_config_rows(pool) -> None:
    await pool.execute("INSERT INTO config (guild_id, key, value) VALUES (7, 'log_channel', '9')")
    table, row = (await botdata_rows(pool))["config:7:log_channel"]
    page = {"id": "pg1", "properties": {
        "Key": {"title": [{"plain_text": "config:7:log_channel"}]},
        "Table": {"select": {"name": table}},
        "Payload": {"rich_text": [{"plain_text": __import__("json").dumps(row)}]},
    }}
    await pool.execute("DELETE FROM config")
    async with pool.acquire() as conn:
        assert await restore_botdata(conn, [page]) == 1
    assert await pool.fetchval("SELECT value FROM config WHERE guild_id = 7") == "9"
    page_id = await pool.fetchval(
        "SELECT notion_page_id FROM notion_mirror WHERE entity='botdata'"
    )
    assert page_id == "pg1"


async def test_rebuild_restores_parents_before_children(pool) -> None:
    import json

    def page(page_id: str, table: str, key: str, row: dict) -> dict:
        return {"id": page_id, "properties": {
            "Key": {"title": [{"plain_text": key}]},
            "Table": {"select": {"name": table}},
            "Payload": {"rich_text": [{"plain_text": json.dumps(row)}]},
        }}

    contact = page("c1", "outreach_contacts", "outreach_contacts:E1:5",
                   {"event_id": "E1", "discord_user_id": 5, "status": "sent"})
    sequence = page("s1", "outreach_sequences", "outreach_sequences:E1", {"event_id": "E1"})
    async with pool.acquire() as conn:
        assert await restore_botdata(conn, [contact, sequence]) == 2
    assert await pool.fetchval("SELECT count(*) FROM outreach_contacts") == 1
