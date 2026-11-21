"""Real-SQL checks of the rebuild from Notion (bot/sync/rebuild.py)."""
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bot.db import djs
from bot.db.events import get_event
from bot.services.logo import LogoError
from bot.sync import rebuild
from tests.sync.pages import dj_page, event_page, slot_page, snapshot

A = 111111111111111111
B = 222222222222222222


def _botdata_page(page_id: str, table: str, key: str, row: dict) -> dict:
    return {"id": page_id, "properties": {
        "Key": {"title": [{"plain_text": key}]},
        "Table": {"select": {"name": table}},
        "Payload": {"rich_text": [{"plain_text": json.dumps(row)}]},
    }}


def _ctx(pool, snap) -> SimpleNamespace:  # noqa: ANN001
    client = MagicMock()
    client.read_all = AsyncMock(return_value=snap)
    return SimpleNamespace(
        db=pool, notion_client=client, maintenance=None, sync_cycle=None, mirror=None,
        log_service=SimpleNamespace(log_notice=AsyncMock(), log_attention=AsyncMock()),
        rebuild_report=None,
    )


async def _seed_old(pool) -> None:  # noqa: ANN001
    await djs.insert_dj(
        pool, discord_user_id=B, dj_name="Old DJ", vrchat_name=None, logo_url=None,
        stream_url_primary=None, stream_url_backup=None, logo_image=b"old-logo",
    )
    await pool.execute("INSERT INTO web_admins (discord_user_id) VALUES (7), (8)")
    await pool.execute("INSERT INTO config (guild_id, key, value) VALUES (5, 'log_channel', '1')")


async def _state(pool) -> dict:  # noqa: ANN001
    return {
        t: sorted(tuple(r.values()) for r in await pool.fetch(f"SELECT * FROM {t}"))  # noqa: S608
        for t in ("djs", "events", "slot_locks", "web_admins", "config")
    }


async def test_rebuild_replaces_data_keeps_admins(pool, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    await _seed_old(pool)

    async def download(url, *, client=None):  # noqa: ANN001, ANN202
        if "broken" in url:
            raise LogoError("download_failed")
        return b"new-logo"

    monkeypatch.setattr(rebuild, "download_logo_file", download)
    snap = dataclasses.replace(
        snapshot(
            events=[event_page("ev-1", "Rave One", slots=2)],
            slots=[slot_page("sl-1", "ev-1", 1, "dj-a")],
            djs=[
                dj_page("dj-a", "DJ A", A, logo="https://files.example/a.png"),
                dj_page("dj-c", "DJ C", 333, logo="https://files.example/broken.png"),
            ],
        ),
        botdata=[_botdata_page(
            "bd-1", "config", "config:5:log_channel",
            {"guild_id": 5, "key": "log_channel", "value": "42"},
        )],
    )
    ctx = _ctx(pool, snap)
    result = await rebuild.rebuild_from_notion(ctx)
    assert result.ok, result.message
    assert list(tmp_path.glob("*before-rebuild*"))  # safety export first

    assert await djs.get_dj_by_discord_id(pool, B) is None  # replaced
    ev = await get_event(pool, "ev-1")
    assert [s.discord_id for s in ev.slots] == [A, None]
    row = await djs.get_dj_by_discord_id(pool, A)
    assert row["logo_image"] == b"new-logo" and row["logo_synced"] is True
    broken = await djs.get_dj_by_discord_id(pool, 333)
    assert broken["logo_image"] is None and broken["logo_synced"] is True
    # Bot data restored, admin accounts untouched
    assert await pool.fetchval("SELECT value FROM config WHERE guild_id = 5") == "42"
    admins = await pool.fetch("SELECT discord_user_id FROM web_admins ORDER BY 1")
    assert [r["discord_user_id"] for r in admins] == [7, 8]
    assert await pool.fetchval(
        "SELECT value FROM config WHERE guild_id = 0 AND key = $1", rebuild.PG_TRUTH_IMPORTED_KEY
    ) == "1"
    what = ctx.log_service.log_notice.await_args.args[1]
    assert "DJ C" in what  # the failed logo is listed
    assert ctx.maintenance is None


async def test_rebuild_clears_post_restore_flag(pool, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(rebuild, "download_logo_file", AsyncMock(return_value=b"x"))
    from bot.sync.notion_import import POST_RESTORE_KEY

    await pool.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (0, $1, '1')", POST_RESTORE_KEY
    )
    snap = snapshot(events=[event_page("ev-1", "Rave One", slots=1)], djs=[dj_page("d", "D", A)])
    assert (await rebuild.rebuild_from_notion(_ctx(pool, snap))).ok
    flag = await pool.fetchval(
        "SELECT value FROM config WHERE guild_id = 0 AND key = $1", POST_RESTORE_KEY
    )
    assert flag != "1"


async def test_rebuild_failure_keeps_old_data(pool, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    await _seed_old(pool)
    before = await _state(pool)
    download = AsyncMock()
    monkeypatch.setattr(rebuild, "download_logo_file", download)
    # Integrity problem: a booked slot whose number is outside the event
    snap = snapshot(
        events=[event_page("ev-1", "Rave One", slots=1)],
        slots=[slot_page("sl-1", "ev-1", 4, "dj-a")],
        djs=[dj_page("dj-a", "DJ A", A, logo="https://files.example/a.png")],
    )
    ctx = _ctx(pool, snap)
    result = await rebuild.rebuild_from_notion(ctx)
    assert not result.ok
    assert await _state(pool) == before
    download.assert_not_awaited()
    ctx.log_service.log_attention.assert_awaited_once()
    assert ctx.maintenance is None

    # A failure after the tables were emptied rolls the whole transaction back.
    import asyncpg

    from bot.sync import notion_import

    async def broken_restore(conn, pages):  # noqa: ANN001, ANN202
        raise asyncpg.PostgresError("disk gone")

    monkeypatch.setattr(notion_import, "restore_botdata", broken_restore)
    good = snapshot(events=[event_page("ev-1", "Rave One", slots=1)], djs=[dj_page("d", "D", A)])
    assert not (await rebuild.rebuild_from_notion(_ctx(pool, good))).ok
    assert await _state(pool) == before


async def test_startup_with_empty_pg_rebuilds_and_marks_the_upgrade_done(
    pool, tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    """Total loss: no upgrade mark, no rows; Notion holds the data (R19)."""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(rebuild, "download_logo_file", AsyncMock(return_value=b"logo"))
    snap = snapshot(
        events=[event_page("ev-1", "Rave One", slots=1)],
        slots=[slot_page("sl-1", "ev-1", 1, "dj-a")],
        djs=[dj_page("dj-a", "DJ A", A, logo="https://files.example/a.png")],
    )
    ctx = _ctx(pool, snap)
    ctx.notion_client.has_rows = AsyncMock(return_value=True)
    ctx.sync_problem = None
    assert await rebuild.run_startup_rebuild_if_needed(ctx) is True
    assert await pool.fetchval(
        "SELECT value FROM config WHERE guild_id = 0 AND key = $1", rebuild.PG_TRUTH_IMPORTED_KEY
    ) == "1"
    assert (await get_event(pool, "ev-1")).slots[0].discord_id == A
    assert ctx.maintenance is None and ctx.rebuild_report is not None
    # The upgrade import then has nothing to do
    assert await rebuild.run_startup_rebuild_if_needed(ctx) is False


async def test_total_loss_finds_the_notion_databases_and_rebuilds(
    pool, tmp_path, monkeypatch  # noqa: ANN001
) -> None:
    """R18 + R19: Postgres is recreated empty; the bot finds its databases and rebuilds."""
    import datetime

    from bot.notion.api import NotionApi
    from bot.notion.client import NotionClient
    from bot.notion.schema import ensure_schema
    from bot.sync.payloads import dj_payload, event_payload, slot_payload
    from tests.notion.fake_notion import FakeNotion
    from tests.pg.conftest import TABLES

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    fake = FakeNotion()
    log = MagicMock()
    log.post_error = AsyncMock()
    log.log_attention = AsyncMock()
    api = NotionApi("key", log, transport=fake.transport)
    ids = await ensure_schema(api, pool, 0, "parent")
    client = NotionClient(api, ids)
    event_page_id = await client.mirror_event("uid-1", event_payload({
        "event_id": "Rave One", "date": datetime.date(2030, 1, 2), "start": "20:00",
        "slot_minutes": 60, "slot_count": 2, "status": None, "genre": None, "notes": None,
    }), None)
    dj_page_id = await client.mirror_dj(dj_payload({
        "discord_user_id": A, "dj_name": "DJ A", "vrchat_name": None,
        "stream_url_primary": None, "stream_url_backup": None,
    }), None, logo_image=None, upload_logo=False)
    await client.mirror_slot(slot_payload("uid-1", 0, A), event_page_id, dj_page_id, None)

    async with pool.acquire() as conn:  # Postgres lost: every table empty, ids gone
        await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    databases_before = len(fake.databases)

    found = await ensure_schema(api, pool, 0, "parent")
    assert found == ids and len(fake.databases) == databases_before
    ctx = _ctx(pool, None)
    ctx.notion_client = NotionClient(api, found)
    ctx.sync_problem = None
    assert await rebuild.run_startup_rebuild_if_needed(ctx) is True
    assert ctx.maintenance is None, ctx.sync_problem
    ev = await get_event(pool, "uid-1")
    assert ev.event_id == "Rave One" and [s.discord_id for s in ev.slots] == [A, None]
    assert await pool.fetchval(
        "SELECT value FROM config WHERE guild_id = 0 AND key = $1", rebuild.PG_TRUTH_IMPORTED_KEY
    ) == "1"
    await api.close()
