"""Real-SQL checks of bot/backup/export.py."""
import base64
import datetime
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


# ---- import (bot/backup/restore.py) ----

import gzip as _gzip  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from bot.backup import restore  # noqa: E402


def _ctx(pool, *, notion: bool = False):  # noqa: ANN001, ANN202
    return SimpleNamespace(
        db=pool, maintenance=None, admins=SimpleNamespace(load=AsyncMock()),
        sync_cycle=SimpleNamespace(resync_all=AsyncMock(return_value=3)) if notion else None,
    )


async def _snapshot(pool) -> dict:  # noqa: ANN001
    doc = await _load(pool)
    return {"tables": doc["tables"], "sequences": doc["sequences"]}


async def _seed(pool) -> None:  # noqa: ANN001
    await pool.execute(
        "INSERT INTO djs (discord_user_id, dj_name, logo_image) VALUES ($1, 'Eve', $2)",
        A, b"\x00\x01\xff",
    )
    await pool.execute("INSERT INTO web_admins (discord_user_id) VALUES (7)")
    await pool.execute(
        "INSERT INTO setup_drafts (guild_id, message_id, channel_id, step, data, updated_at) "
        "VALUES (1, 2, 3, 1, $1::jsonb, now())",
        json.dumps({"k": [1, 2]}),
    )
    await pool.execute("SELECT nextval('guest_dj_id_seq')")
    from bot.db.events import EventDraft, insert_event

    await insert_event(
        pool, EventDraft("E1", datetime.date(2030, 1, 2), datetime.time(20, 0), 60, 3), uid="u1"
    )
    await pool.execute("INSERT INTO outreach_sequences (event_id) VALUES ('E1')")
    await pool.execute(
        "INSERT INTO outreach_contacts (event_id, discord_user_id) VALUES ('E1', $1)", A
    )


async def test_import_round_trip(pool, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    await _seed(pool)
    before = await _snapshot(pool)
    data = await export_database(pool)
    await pool.execute("DELETE FROM djs")
    await pool.execute("INSERT INTO djs (discord_user_id, dj_name) VALUES (5, 'Other')")
    await pool.execute("SELECT nextval('guest_dj_id_seq')")
    ctx = _ctx(pool)
    result = await restore.import_database(ctx, data)
    assert result.ok, result.message
    assert await _snapshot(pool) == before
    assert ctx.maintenance is None
    assert list(tmp_path.glob("*before-import*"))


async def test_import_refuses_newer_revision(pool, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    await _seed(pool)
    doc = json.loads(_gzip.decompress(await export_database(pool)))
    doc["alembic_revision"] = "9999_future"
    before = await _snapshot(pool)
    result = await restore.import_database(_ctx(pool), _gzip.compress(json.dumps(doc).encode()))
    assert not result.ok and "9999_future" in result.message
    assert await _snapshot(pool) == before
    assert not list(tmp_path.iterdir())


async def test_import_corrupt_file_leaves_db(pool, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    await _seed(pool)
    before = await _snapshot(pool)
    ctx = _ctx(pool)
    assert not (await restore.import_database(ctx, b"garbage")).ok
    # A failure in the middle of the write rolls everything back.
    doc = json.loads(_gzip.decompress(await export_database(pool)))
    doc["tables"]["djs"]["rows"].append(doc["tables"]["djs"]["rows"][0])  # duplicate key
    result = await restore.import_database(ctx, _gzip.compress(json.dumps(doc).encode()))
    assert not result.ok
    assert await _snapshot(pool) == before
    assert ctx.maintenance is None


async def test_import_reloads_admins_and_resyncs(pool, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    await _seed(pool)
    data = await export_database(pool)
    ctx = _ctx(pool, notion=True)
    assert (await restore.import_database(ctx, data)).ok
    ctx.admins.load.assert_awaited_once()
    ctx.sync_cycle.resync_all.assert_awaited_once()
    plain = _ctx(pool)
    assert (await restore.import_database(plain, data)).ok
    plain.admins.load.assert_awaited_once()
