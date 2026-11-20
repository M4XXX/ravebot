"""Real-SQL checks of bot/db/events.py."""
import datetime

import pytest

from bot.db import bookings, djs
from bot.db.events import (
    DuplicateEventError,
    EventDraft,
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
