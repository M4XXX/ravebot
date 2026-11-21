"""Real-SQL checks of bot/db/events.py."""
import datetime

import pytest

from bot.db import bookings, djs
from bot.db.events import (
    DuplicateEventError,
    EventDraft,
    SlotsBookedError,
    edit_event,
    get_event,
    insert_event,
    list_events,
    set_event_status,
    update_event,
)
from bot.timezone import local_today

A = 111111111111111111


def _draft(event_id, date, start=datetime.time(20, 0), slots=3):
    return EventDraft(
        event_id=event_id, date=date, start=start, slot_minutes=60, slot_count=slots
    )


async def test_list_events_matches_notion_shape(pool) -> None:
    await djs.insert_dj(
        pool, discord_user_id=A, dj_name="DJ Test", vrchat_name=None, logo_url=None,
        stream_url_primary="https://twitch.tv/x", stream_url_backup=None,
        active_booking=True,
    )
    day = local_today() + datetime.timedelta(days=3)
    created = await insert_event(pool, _draft("E1", day, slots=3), uid="u1")
    await bookings.acquire_slot_lock(pool, "u1", 1, A)
    events = await list_events(pool)
    assert len(events) == 1
    ev = events[0]
    assert (ev.page_id, ev.event_id, ev.date, ev.slot_count) == ("u1", "E1", day, 3)
    assert ev.start == datetime.time(20, 0) and ev.slot_minutes == 60
    assert ev.status is None and ev.genre is None and ev.notes is None
    assert [s.number for s in ev.slots] == [1, 2, 3]
    assert ev.slots[0].discord_id is None and ev.slots[0].dj_page_id is None
    booked = ev.slots[1]
    assert booked.discord_id == A and booked.dj_name == "DJ Test"
    assert booked.dj_page_id == str(A)
    assert booked.page_id == "u1:2" and booked.extra_dj_page_ids == ()
    assert created.slots[0].page_id == "u1:1"
    assert await get_event(pool, "u1") == ev
    assert await get_event(pool, "nope") is None


async def test_list_events_hides_past_unless_asked(pool) -> None:
    today = local_today()
    await insert_event(pool, _draft("OLD", today - datetime.timedelta(days=1)), uid="o")
    await insert_event(
        pool, _draft("LATE", today, start=datetime.time(22, 0)), uid="l"
    )
    await insert_event(pool, _draft("EARLY", today, start=datetime.time(18, 0)), uid="e")
    assert [e.event_id for e in await list_events(pool)] == ["EARLY", "LATE"]
    assert [e.event_id for e in await list_events(pool, include_past=True)] == [
        "OLD", "EARLY", "LATE",
    ]


async def test_insert_duplicate_event_id_raises(pool) -> None:
    day = local_today()
    await insert_event(pool, _draft("DUP", day))
    with pytest.raises(DuplicateEventError):
        await insert_event(pool, _draft("DUP", day))
    assert len(await list_events(pool)) == 1


async def test_insert_event_creates_slots(pool) -> None:
    ev = await insert_event(pool, _draft("S", local_today(), slots=4))
    n = await pool.fetchval(
        "SELECT count(*) FROM slot_locks WHERE event_id = $1", ev.page_id
    )
    assert n == 4 and len(ev.slots) == 4
    assert ev.page_id  # generated uuid


async def test_update_and_status(pool) -> None:
    await insert_event(pool, _draft("U", local_today()), uid="u")
    assert await update_event(pool, "u", genre="House", start=datetime.time(21, 30))
    await set_event_status(pool, "u", "Done")
    ev = await get_event(pool, "u")
    assert ev.genre == "House" and ev.start == datetime.time(21, 30)
    assert ev.status == "Done"
    assert not await update_event(pool, "missing", genre="x")


async def _booked_event(pool, slots=4, booked_index=1):
    await djs.insert_dj(
        pool, discord_user_id=A, dj_name="DJ Test", vrchat_name=None, logo_url=None,
        stream_url_primary="https://twitch.tv/x", stream_url_backup=None,
        active_booking=True,
    )
    day = local_today() + datetime.timedelta(days=3)
    await insert_event(pool, _draft("E1", day, slots=slots), uid="u1")
    if booked_index is not None:
        await bookings.acquire_slot_lock(pool, "u1", booked_index, A)


async def _slot_rows(pool):
    rows = await pool.fetch(
        "SELECT slot_index FROM slot_locks WHERE event_id = 'u1' ORDER BY slot_index"
    )
    return [r["slot_index"] for r in rows]


async def test_edit_event_grows_and_shrinks_slot_rows(pool) -> None:
    await _booked_event(pool, slots=4, booked_index=1)
    assert await edit_event(pool, "u1", slot_count=6, genre="Techno") is True
    assert await _slot_rows(pool) == [0, 1, 2, 3, 4, 5]
    assert (await get_event(pool, "u1")).genre == "Techno"
    assert await edit_event(pool, "u1", slot_count=2) is True
    assert await _slot_rows(pool) == [0, 1]
    assert (await get_event(pool, "u1")).slot_count == 2
    assert await edit_event(pool, "nope", genre="x") is False


async def test_edit_event_refuses_cutting_a_booked_slot(pool) -> None:
    await _booked_event(pool, slots=4, booked_index=2)
    with pytest.raises(SlotsBookedError) as caught:
        await edit_event(pool, "u1", slot_count=2)
    assert caught.value.booked == (2,)
    assert await _slot_rows(pool) == [0, 1, 2, 3]
    assert (await get_event(pool, "u1")).slot_count == 4


async def test_edit_event_refuses_moving_times_of_booked_slots(pool) -> None:
    await _booked_event(pool, slots=4, booked_index=0)
    with pytest.raises(SlotsBookedError):
        await edit_event(pool, "u1", start=datetime.time(21, 0))
    with pytest.raises(SlotsBookedError):
        await edit_event(pool, "u1", slot_minutes=45)
    assert await edit_event(pool, "u1", start=datetime.time(20, 0), slot_minutes=60) is True


async def test_edit_event_moves_times_when_nothing_is_booked(pool) -> None:
    await _booked_event(pool, slots=4, booked_index=None)
    assert await edit_event(pool, "u1", start=datetime.time(21, 30), slot_minutes=45) is True
    ev = await get_event(pool, "u1")
    assert ev.start == datetime.time(21, 30) and ev.slot_minutes == 45


async def test_location_and_markers_round_trip(pool) -> None:
    day = local_today() + datetime.timedelta(days=3)
    draft = EventDraft(
        event_id="E9", date=day, start=datetime.time(20, 0), slot_minutes=60,
        slot_count=2, location="Club Vortex", genre="Techno", genre_by_hand=True,
    )
    await insert_event(pool, draft, uid="u9")
    ev = await get_event(pool, "u9")
    assert ev.location == "Club Vortex"
    assert ev.genre == "Techno"
    assert ev.genre_by_hand is True
    assert ev.location_by_hand is False
    assert await update_event(pool, "u9", location=None, location_by_hand=False)
    assert (await get_event(pool, "u9")).location is None
