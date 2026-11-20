"""Real-SQL checks of bot/backup/export.py."""
import base64
import gzip
import json

from bot.backup.export import export_database

A = 111111111111111111


async def _load(pool) -> dict:  # noqa: ANN001
    return json.loads(gzip.decompress(await export_database(pool)))


async def test_export_contains_every_table_but_alembic(pool) -> None:  # noqa: ANN001
    doc = await _load(pool)
    expected = {
        r["table_name"]
        for r in await pool.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    } - {"alembic_version"}
    assert set(doc["tables"]) == expected
    assert doc["format"] == "ravebot-backup" and doc["version"] == 1
    assert doc["alembic_revision"] == await pool.fetchval("SELECT version_num FROM alembic_version")
    assert "guest_dj_id_seq" in doc["sequences"]
    seq_names = {r["sequencename"] for r in await pool.fetch(
        "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")}
    assert set(doc["sequences"]) == seq_names


async def test_export_encodes_bytea_and_jsonb(pool) -> None:  # noqa: ANN001
    await pool.execute(
        "INSERT INTO djs (discord_user_id, dj_name, logo_image) VALUES ($1, 'Eve', $2)",
        A, b"\x00\x01\xff",
    )
    await pool.execute("SELECT nextval('guest_dj_id_seq')")
    doc = await _load(pool)
    table = doc["tables"]["djs"]
    names = [c["name"] for c in table["columns"]]
    types = {c["name"]: c["type"] for c in table["columns"]}
    assert types["logo_image"] == "bytea"
    (row,) = table["rows"]
    assert base64.b64decode(row[names.index("logo_image")]) == b"\x00\x01\xff"
    assert row[names.index("dj_name")] == "Eve"
    assert doc["sequences"]["guest_dj_id_seq"]["is_called"] is True


async def test_export_decodes_jsonb_and_timestamps(pool) -> None:  # noqa: ANN001
    await pool.execute(
        "INSERT INTO setup_drafts (guild_id, message_id, channel_id, step, data, updated_at) "
        "VALUES (1, 2, 3, 1, $1::jsonb, now())",
        json.dumps({"k": [1, 2]}),
    )
    table = (await _load(pool))["tables"]["setup_drafts"]
    names = [c["name"] for c in table["columns"]]
    types = {c["name"]: c["type"] for c in table["columns"]}
    assert types["data"] == "jsonb" and types["updated_at"] == "timestamp with time zone"
    (row,) = table["rows"]
    assert row[names.index("data")] == {"k": [1, 2]}
    assert "T" in row[names.index("updated_at")]
