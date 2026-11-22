"""Real-SQL checks of the slot limit inside acquire_slot_lock (bot/db/bookings.py)."""
import asyncio
import datetime

import pytest

from bot.db import djs
from bot.db.bookings import DjSlotLimitReached, acquire_slot_lock, get_slot_lock_holder
from bot.db.events import EventDraft, insert_event
from bot.timezone import local_today

A = 111111111111111111
B = 222222222222222222


async def _dj(pool, user_id: int, name: str) -> None:
    await djs.insert_dj(
        pool, discord_user_id=user_id, dj_name=name, vrchat_name=None, logo_url=None,
        stream_url_primary="https://twitch.tv/x", stream_url_backup=None,
        active_booking=True,
    )


async def _event(pool, uid: str, slots: int = 4) -> None:
    day = local_today() + datetime.timedelta(days=3)
    await insert_event(
        pool,
        EventDraft(
            event_id=uid.upper(), date=day, start=datetime.time(20, 0),
            slot_minutes=60, slot_count=slots,
        ),
        uid=uid,
    )


@pytest.fixture
async def setup(pool):
    await _dj(pool, A, "DJ A")
    await _dj(pool, B, "DJ B")
    await _event(pool, "e1")
    await _event(pool, "e2")
    return pool


async def test_third_slot_refused_at_limit_two(setup) -> None:
    pool = setup
    assert await acquire_slot_lock(pool, "e1", 0, A, max_per_dj=2) is True
    assert await acquire_slot_lock(pool, "e1", 1, A, max_per_dj=2) is True
    with pytest.raises(DjSlotLimitReached):
        await acquire_slot_lock(pool, "e1", 2, A, max_per_dj=2)
    assert await get_slot_lock_holder(pool, "e1", 2) is None


async def test_no_limit_without_max(setup) -> None:
    pool = setup
    for index in range(3):
        assert await acquire_slot_lock(pool, "e1", index, A) is True


async def test_concurrent_claims_of_different_slots_one_wins(setup) -> None:
    pool = setup
    results = await asyncio.gather(
        acquire_slot_lock(pool, "e1", 0, A, max_per_dj=1),
        acquire_slot_lock(pool, "e1", 1, A, max_per_dj=1),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["DjSlotLimitReached", "bool"]
    assert [r for r in results if r is True] == [True]
    held = await pool.fetchval(
        "SELECT count(*) FROM slot_locks WHERE event_id = 'e1' AND discord_user_id = $1", A
    )
    assert held == 1


async def test_other_dj_and_other_event_unaffected(setup) -> None:
    pool = setup
    assert await acquire_slot_lock(pool, "e1", 0, A, max_per_dj=1) is True
    assert await acquire_slot_lock(pool, "e1", 1, B, max_per_dj=1) is True
    assert await acquire_slot_lock(pool, "e2", 0, A, max_per_dj=1) is True


async def test_slot_beyond_slot_count_does_not_count(setup) -> None:
    pool = setup
    assert await acquire_slot_lock(pool, "e1", 3, A, max_per_dj=1) is True
    # The event shrank: slot index 3 is no longer part of it (row left behind)
    await pool.execute("UPDATE events SET slot_count = 3 WHERE uid = 'e1'")
    assert await acquire_slot_lock(pool, "e1", 0, A, max_per_dj=1) is True
