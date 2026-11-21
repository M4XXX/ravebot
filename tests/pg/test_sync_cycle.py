"""Real-SQL checks of bot/sync/cycle.py: Postgres payloads, a conflict, resync_all."""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bot.db import bookings, djs, issues, mirror, outbox
from bot.db.config import get_value
from bot.db.events import EventDraft, insert_event
from bot.sync.cycle import SyncCycle, _load_pg
from bot.sync.payloads import event_payload
from bot.timezone import local_today
from tests.sync.pages import dj_page, event_page, slot_page, snapshot

A = 111111111111111111


async def _seed(pool) -> datetime.date:  # noqa: ANN001
    await djs.insert_dj(
        pool, discord_user_id=A, dj_name="DJ A", vrchat_name=None, logo_url=None,
        stream_url_primary="https://twitch.tv/a", stream_url_backup=None, active_booking=True,
    )
    day = local_today() + datetime.timedelta(days=3)
    await insert_event(
        pool,
        EventDraft(event_id="E1", date=day, start=datetime.time(20, 0),
                   slot_minutes=60, slot_count=2),
        uid="u1",
    )
    await bookings.acquire_slot_lock(pool, "u1", 1, A)
    return day


def _ctx(pool, client=None):  # noqa: ANN001, ANN202
    log = MagicMock(log_attention=AsyncMock())
    return SimpleNamespace(
        db=pool, notion_client=client, log_service=log, maintenance=None, mirror=MagicMock()
    )


async def test_load_pg_payloads(pool) -> None:
    day = await _seed(pool)
    assert await _load_pg(pool, "event") == {
        "u1": {
            "event_id": "E1", "date": day.isoformat(), "start": "20:00", "slot_minutes": 60,
            "slot_count": 2, "status": None, "genre": None, "notes": None, "location": None,
        }
    }
    assert await _load_pg(pool, "dj") == {
        str(A): {
            "discord_id": A, "dj_name": "DJ A", "vrchat_name": None,
            "link": "https://twitch.tv/a", "alt_link": None,
        }
    }
    assert await _load_pg(pool, "slot") == {
        "u1:0": {"event_uid": "u1", "number": 1, "discord_id": None},
        "u1:1": {"event_uid": "u1", "number": 2, "discord_id": A},
    }


async def test_load_pg_slot_of_deleted_dj_is_free_for_notion(pool) -> None:
    """I3: a booking whose DJ row is gone is compared as Notion holds it (no DJ)."""
    await _seed(pool)
    await pool.execute("DELETE FROM djs WHERE discord_user_id = $1", A)
    assert (await _load_pg(pool, "slot"))["u1:1"] == {
        "event_uid": "u1", "number": 2, "discord_id": None,
    }
    assert await bookings.get_slot_lock_holder(pool, "u1", 1) == A  # the booking stays



async def test_resync_all_queues_every_record(pool) -> None:
    await _seed(pool)
    assert await SyncCycle(_ctx(pool), MagicMock()).resync_all() == 4
    assert {(r.entity, r.entity_key) for r in await outbox.due(pool, 10)} == {
        ("event", "u1"), ("dj", str(A)), ("slot", "u1:0"), ("slot", "u1:1"),
    }


async def test_conflict_recorded_once(pool) -> None:
    day = await _seed(pool)
    synced = event_payload(
        {"event_id": "E1", "date": day, "start": "20:00", "slot_minutes": 60,
         "slot_count": 2, "status": None, "genre": None, "notes": None, "location": None}
    )
    await mirror.set_synced(pool, "event", "u1", "pa", synced)
    await mirror.set_synced(pool, "dj", str(A), "pdj", (await _load_pg(pool, "dj"))[str(A)])
    for key, payload in (await _load_pg(pool, "slot")).items():
        await mirror.set_synced(pool, "slot", key, f"ps{key[-1]}", payload)
    await pool.execute("UPDATE events SET genre = 'Techno' WHERE uid = 'u1'")
    client = MagicMock()
    client.read_all = AsyncMock(return_value=snapshot(
        [event_page("pa", "E1", day.isoformat(), slots=2, start="21:00")],
        [slot_page("ps0", "pa", 1), slot_page("ps1", "pa", 2, "pdj")],
        [dj_page("pdj", "DJ A", A, link="https://twitch.tv/a")],
    ))
    cycle = SyncCycle(_ctx(pool, client), MagicMock())

    report = await cycle.run()
    again = await cycle.run()

    assert (report.conflicts, report.warnings, report.applied) == (1, 0, 0)
    assert again.conflicts == 0
    (issue,) = await issues.list_open(pool)
    assert (issue.kind, issue.entity, issue.entity_key) == ("conflict", "event", "u1")
    assert issue.pg_payload["genre"] == "Techno" and issue.notion_payload["start"] == "21:00"
    assert await mirror.open_issue_keys(pool) == {("event", "u1")}
    assert await get_value(pool, 0, "sync_last_error") == ""
    assert await get_value(pool, 0, "sync_last_run")


async def _synced_everything(pool) -> None:  # noqa: ANN001
    for entity in ("event", "dj", "slot"):
        for key, payload in (await _load_pg(pool, entity)).items():
            await mirror.set_synced(pool, entity, key, f"p-{key}", payload)


def _client_for(day, **event) -> MagicMock:  # noqa: ANN001
    client = MagicMock()
    client.read_all = AsyncMock(return_value=snapshot(
        [event_page("p-u1", "E1", day.isoformat(), slots=2, **event)],
        [slot_page("p-u1:0", "p-u1", 1), slot_page("p-u1:1", "p-u1", 2, "p-111111111111111111")],
        [dj_page("p-111111111111111111", "DJ A", A, link="https://twitch.tv/a")],
    ))
    return client


async def test_mirrored_rule_value_is_not_a_hand_edit(pool) -> None:
    day = await _seed(pool)
    await pool.execute("UPDATE events SET location = 'Club Nebula' WHERE uid = 'u1'")
    await _synced_everything(pool)  # what the worker mirrored
    cycle = SyncCycle(_ctx(pool, _client_for(day, location="Club Nebula")), MagicMock())

    report = await cycle.run()

    assert (report.applied, report.conflicts) == (0, 0)
    row = await pool.fetchrow("SELECT location, location_by_hand FROM events WHERE uid = 'u1'")
    assert (row["location"], row["location_by_hand"]) == ("Club Nebula", False)


async def test_notion_location_edit_through_the_cycle_sets_the_marker(pool) -> None:
    day = await _seed(pool)
    await _synced_everything(pool)
    cycle = SyncCycle(_ctx(pool, _client_for(day, location="Basement")), MagicMock())

    report = await cycle.run()

    assert report.applied == 1
    row = await pool.fetchrow("SELECT location, location_by_hand FROM events WHERE uid = 'u1'")
    assert (row["location"], row["location_by_hand"]) == ("Basement", True)
