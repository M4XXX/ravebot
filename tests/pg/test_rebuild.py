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
