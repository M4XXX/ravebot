"""Real-SQL checks of the Notion import (bot/sync/notion_import.py)."""
import dataclasses
import datetime
from unittest.mock import AsyncMock, MagicMock

from bot.db import djs, mirror
from bot.db.events import get_event, list_events
from bot.sync.notion_import import import_from_notion
from bot.sync.payloads import payload_hash
from tests.sync.pages import dj_page, event_page, slot_page, snapshot

A = 111111111111111111
B = 222222222222222222


def _client(snap) -> MagicMock:  # noqa: ANN001
    client = MagicMock()
    client.read_all = AsyncMock(return_value=snap)
    return client


def _notion() -> object:
    return snapshot(
        events=[
            event_page("ev-1", "Rave One", "2026-12-01", slots=3, genre="Techno"),
            event_page("ev-old", "Old Rave", "2020-01-01", slots=2),  # past events too
            event_page("ev-bad", None),  # unreadable: skipped
        ],
        slots=[
            slot_page("sl-1", "ev-1", 1, "dj-a"),
            slot_page("sl-2", "ev-1", 2),
            slot_page("sl-3", "ev-1", 3, "dj-guest"),
            slot_page("sl-4", "ev-old", 2, "dj-b"),
            slot_page("sl-5", "ev-1", 9),  # empty row above slot_count: skipped
        ],
        djs=[
            dj_page("dj-a", "DJ A", A, vrchat="a_vr", link="https://twitch.tv/a"),
            dj_page("dj-b", "DJ B", B),
            dj_page("dj-guest", "Guest G"),  # no Discord ID: gets a guest id
            dj_page("dj-old-guest", "Old Guest", -7),
        ],
    )


async def _count(pool, table: str) -> int:  # noqa: ANN001
    return await pool.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608


async def test_upgrade_import_copies_events_slots_djs(pool) -> None:
    result = await import_from_notion(pool, _client(_notion()), mode="upgrade")
    assert result.ok, result.problems
    assert (result.events, result.slots, result.djs) == (2, 4, 4)
    assert result.notes  # the unreadable event and the stray slot row

    ev = await get_event(pool, "ev-1")
    assert ev.event_id == "Rave One" and ev.genre == "Techno" and ev.slot_count == 3
    assert ev.date == datetime.date(2026, 12, 1)
    guest_id = await pool.fetchval("SELECT discord_user_id FROM djs WHERE dj_name = 'Guest G'")
    assert guest_id < -7  # below the imported guest ids
    assert [s.discord_id for s in ev.slots] == [A, None, guest_id]
    old = [e for e in await list_events(pool, include_past=True) if e.page_id == "ev-old"]
    assert [s.discord_id for s in old[0].slots] == [None, B]

    row = await djs.get_dj_by_discord_id(pool, A)
    assert row["dj_name"] == "DJ A" and row["vrchat_name"] == "a_vr"
    assert row["stream_url_primary"] == "https://twitch.tv/a"
    assert row["active_booking"] is False  # Notion-only DJs were never invited before
    assert await djs.get_dj_by_discord_id(pool, -7) is not None

    m = await mirror.get(pool, "event", "ev-1")
    assert m.notion_page_id == "ev-1" and m.synced_payload["event_id"] == "Rave One"
    slot = await mirror.get(pool, "slot", "ev-1:2")
    assert slot.notion_page_id == "sl-3"
    assert slot.synced_payload == {"event_uid": "ev-1", "number": 3, "discord_id": guest_id}
    assert (await mirror.get(pool, "dj", str(A))).notion_page_id == "dj-a"
    guest_mirror = await mirror.get(pool, "dj", str(guest_id))
    assert guest_mirror.notion_page_id == "dj-guest"
    assert guest_mirror.synced_payload["discord_id"] is None  # what Notion holds
    queued = {r["entity_key"] for r in await pool.fetch("SELECT entity_key FROM notion_outbox")}
    assert str(guest_id) in queued  # the worker writes the new guest id to Notion
    assert "ev-old:0" in queued  # slot 1 of Old Rave has no Notion row yet


async def test_upgrade_import_keeps_existing_dj_profile(pool) -> None:
    await djs.insert_dj(
        pool, discord_user_id=A, dj_name="DJ A Postgres", vrchat_name=None, logo_url=None,
        stream_url_primary="https://twitch.tv/pg", stream_url_backup=None,
        logo_image=b"logo", active_booking=True,
    )
    await pool.execute(
        "INSERT INTO notion_mirror (entity, entity_key, notion_page_id) VALUES ('dj', $1, 'old')",
        str(A),
    )
    snap = snapshot(djs=[dj_page("dj-a", "DJ A Notion", A, link="https://twitch.tv/notion")])
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok, result.problems
    row = await djs.get_dj_by_discord_id(pool, A)
    assert row["dj_name"] == "DJ A Postgres"
    assert row["stream_url_primary"] == "https://twitch.tv/pg"
    assert row["logo_image"] == b"logo" and row["active_booking"] is True
    m = await mirror.get(pool, "dj", str(A))
    assert m.notion_page_id == "dj-a"
    assert m.synced_payload["dj_name"] == "DJ A Notion"
    assert m.synced_hash == payload_hash(m.synced_payload)
    queued = await pool.fetch("SELECT entity, entity_key FROM notion_outbox")
    assert [(r["entity"], r["entity_key"]) for r in queued] == [("dj", str(A))]


async def test_upgrade_import_merges_existing_slot_locks(pool) -> None:
    for dj_id, name in ((A, "DJ A"), (B, "DJ B")):
        await djs.insert_dj(
            pool, discord_user_id=dj_id, dj_name=name, vrchat_name=None, logo_url=None,
            stream_url_primary=None, stream_url_backup=None,
        )
    # Old rows keyed by the Notion page id: slot 1 agrees, slot 2 differs from Notion
    await pool.execute(
        "INSERT INTO slot_locks (event_id, slot_index, discord_user_id, booked_at)"
        " VALUES ('ev-1', 0, $1, '2026-01-01'), ('ev-1', 1, $2, now())",
        A, B,
    )
    snap = snapshot(
        events=[event_page("ev-1", "Rave One", slots=3)],
        slots=[slot_page("sl-1", "ev-1", 1, "dj-a"), slot_page("sl-2", "ev-1", 2)],
        djs=[dj_page("dj-a", "DJ A", A), dj_page("dj-b", "DJ B", B)],
    )
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok, result.problems
    rows = await pool.fetch(
        "SELECT slot_index, discord_user_id, booked_at FROM slot_locks"
        " WHERE event_id = 'ev-1' ORDER BY slot_index"
    )
    assert [(r["slot_index"], r["discord_user_id"]) for r in rows] == [
        (0, A), (1, None), (2, None),
    ]
    assert rows[0]["booked_at"].year == 2026 and rows[0]["booked_at"].month == 1
    assert rows[1]["booked_at"] is None


async def test_upgrade_import_keeps_the_booking_of_an_unreadable_dj_page(pool) -> None:
    """I6: a booked slot whose Notion DJ page cannot be read keeps the database holder."""
    await djs.insert_dj(
        pool, discord_user_id=A, dj_name="DJ A", vrchat_name=None, logo_url=None,
        stream_url_primary=None, stream_url_backup=None,
    )
    await pool.execute(
        "INSERT INTO slot_locks (event_id, slot_index, discord_user_id, booked_at)"
        " VALUES ('ev-1', 0, $1, now())",
        A,
    )
    snap = snapshot(
        events=[event_page("ev-1", "Rave One", slots=2)],
        slots=[slot_page("sl-1", "ev-1", 1, "dj-unreadable")],
        djs=[dj_page("dj-unreadable", None)],  # no name: skipped as unreadable
    )
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok, result.problems
    holder = await pool.fetchval(
        "SELECT discord_user_id FROM slot_locks WHERE event_id = 'ev-1' AND slot_index = 0"
    )
    assert holder == A
    synced = await mirror.get(pool, "slot", "ev-1:0")
    assert synced is not None and synced.synced_payload["discord_id"] == A
    assert any("kept" in n for n in result.notes)


async def test_upgrade_import_rolls_back_on_unreadable_dj_page_without_booking(pool) -> None:
    """I6: no database holder to keep: the booking would be lost, so nothing is imported."""
    snap = snapshot(
        events=[event_page("ev-1", "Rave One", slots=2)],
        slots=[slot_page("sl-1", "ev-1", 1, "dj-unreadable")],
        djs=[dj_page("dj-unreadable", None)],
    )
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert not result.ok
    assert any("DJ page" in p for p in result.problems)
    assert await _count(pool, "events") == 0


async def test_upgrade_import_rolls_back_on_duplicate_titles(pool) -> None:
    snap = snapshot(
        events=[event_page("p1", "Rave"), event_page("p2", "Rave")],
        djs=[dj_page("dj-a", "DJ A", A)],
    )
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok is False
    assert any("Rave" in p for p in result.problems)
    for table in ("events", "slot_locks", "djs", "notion_mirror", "notion_outbox"):
        assert await _count(pool, table) == 0, table


async def test_upgrade_import_rolls_back_on_orphan_slot(pool) -> None:
    snap = snapshot(
        events=[event_page("p1", "Rave")],
        slots=[slot_page("s1", "p1", 1, "dj-a"), slot_page("s2", "gone", 1, "dj-a")],
        djs=[dj_page("dj-a", "DJ A", A)],
    )
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok is False and result.problems
    for table in ("events", "slot_locks", "djs", "notion_mirror", "notion_outbox"):
        assert await _count(pool, table) == 0, table


async def test_upgrade_import_rolls_back_on_title_taken_in_postgres(pool) -> None:
    await pool.execute(
        "INSERT INTO events (uid, event_id, date, start, slot_minutes, slot_count)"
        " VALUES ('other', 'Rave', '2026-12-01', '20:00', 60, 2)"
    )
    snap = snapshot(events=[event_page("p1", "Rave")], djs=[dj_page("dj-a", "DJ A", A)])
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok is False
    assert any("Rave" in p for p in result.problems)
    assert await _count(pool, "djs") == 0


async def test_upgrade_import_rolls_back_on_dj_name_taken(pool) -> None:
    await djs.insert_dj(
        pool, discord_user_id=B, dj_name="dj  a", vrchat_name=None, logo_url=None,
        stream_url_primary=None, stream_url_backup=None,
    )
    snap = snapshot(events=[event_page("p1", "Rave")], djs=[dj_page("dj-a", "DJ A", A)])
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok is False
    assert any("DJ A" in p for p in result.problems)
    assert await _count(pool, "events") == 0


async def test_replace_import_empties_the_tables_first(pool) -> None:
    await djs.insert_dj(
        pool, discord_user_id=B, dj_name="Gone", vrchat_name=None, logo_url=None,
        stream_url_primary=None, stream_url_backup=None,
    )
    await pool.execute(
        "INSERT INTO notion_outbox (entity, entity_key) VALUES ('event', 'x')"
    )
    await pool.execute(
        "INSERT INTO sync_issues (kind, summary) VALUES ('warning', 'old')"
    )
    snap = snapshot(events=[event_page("p1", "Rave", slots=1)], djs=[dj_page("dj-a", "DJ A", A)])
    result = await import_from_notion(pool, _client(snap), mode="replace")
    assert result.ok, result.problems
    assert await djs.get_dj_by_discord_id(pool, B) is None
    assert await djs.get_dj_by_discord_id(pool, A) is not None
    assert await _count(pool, "sync_issues") == 0
    assert await _count(pool, "notion_outbox") == 1  # slot 1 has no Notion row
    assert await get_event(pool, "p1") is not None


async def test_import_recomputes_markers(pool) -> None:
    import json

    rule = {"id": 1, "guild_id": 7, "field": "location", "value": "Club Nebula", "every": 1,
            "anchor": "2026-12-01", "created_at": "2026-01-01T10:00:00+00:00"}
    rule_page = {"id": "bd-1", "properties": {
        "Key": {"title": [{"plain_text": "schedule_rules:1"}]},
        "Table": {"select": {"name": "schedule_rules"}},
        "Payload": {"rich_text": [{"plain_text": json.dumps(rule)}]},
    }}
    manual = {"page_id": "ev-manual"}
    manual_page = {"id": "bd-2", "properties": {
        "Key": {"title": [{"plain_text": "manual_events:ev-manual"}]},
        "Table": {"select": {"name": "manual_events"}},
        "Payload": {"rich_text": [{"plain_text": json.dumps(manual)}]},
    }}
    snap = dataclasses.replace(
        snapshot(events=[
            event_page("ev-rule", "Rule", "2026-12-01", slots=1, location="Club Nebula"),
            event_page("ev-diff", "Differs", "2026-12-08", slots=1, location="Elsewhere",
                       genre="Techno"),
            event_page("ev-empty", "Empty", "2026-12-15", slots=1),
            event_page("ev-manual", "Manual", "2026-12-22", slots=1, location="Club Nebula"),
        ]),
        botdata=[rule_page, manual_page],
    )
    result = await import_from_notion(pool, _client(snap), mode="replace")
    assert result.ok, result.problems
    rows = {
        r["uid"]: (r["location_by_hand"], r["genre_by_hand"])
        for r in await pool.fetch("SELECT uid, location_by_hand, genre_by_hand FROM events")
    }
    assert rows["ev-rule"] == (False, False)  # equals the rule value
    assert rows["ev-diff"] == (True, True)  # differs from the rule / no genre rule
    assert rows["ev-empty"] == (False, False)
    assert rows["ev-manual"] == (True, False)  # manual: any value is by hand


async def test_upgrade_import_marks_imported_values_by_hand_only_for_new_events(pool) -> None:
    from bot.db.events import EventDraft, insert_event

    await insert_event(pool, EventDraft(
        event_id="Existing", date=datetime.date(2026, 12, 20), start=datetime.time(20, 0),
        slot_minutes=60, slot_count=1, genre="Kept", genre_by_hand=False,
    ), uid="ev-existing")
    snap = snapshot(events=[
        event_page("ev-new", "New", "2026-12-01", slots=1, genre="Techno", location="Club"),
        event_page("ev-plain", "Plain", "2026-12-08", slots=1),
    ])
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok, result.problems
    rows = {
        r["uid"]: (r["genre_by_hand"], r["location_by_hand"])
        for r in await pool.fetch("SELECT uid, genre_by_hand, location_by_hand FROM events")
    }
    assert rows["ev-new"] == (True, True)
    assert rows["ev-plain"] == (False, False)
    assert rows["ev-existing"] == (False, False)  # untouched
