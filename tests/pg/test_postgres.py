"""Real-SQL tests against a disposable PostgreSQL (opt-in).

Everything else in the suite mocks asyncpg, so no SQL statement and no migration ever ran
under test. These tests run only when RAVEBOT_TEST_POSTGRES_URL points at an EMPTY,
throw-away database, e.g.

    docker run --rm -d -p 127.0.0.1:55432:5432 -e POSTGRES_USER=ravebot \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=ravebot_test postgres:16-alpine
    RAVEBOT_TEST_POSTGRES_URL=postgresql://ravebot:test@127.0.0.1:55432/ravebot_test \\
        uv run pytest -m pg

The schema is dropped and recreated with alembic; never point this at real data. As a
guard the database name must end in "_test", anything else is refused before alembic runs.
"""
import asyncio

import pytest

from tests.pg import SKIP_WITHOUT_URL, run_alembic

pytestmark = [pytest.mark.pg, SKIP_WITHOUT_URL]


def test_migrations_go_down_and_up_again() -> None:
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")


async def test_two_djs_racing_for_one_slot_get_exactly_one_lock(pool) -> None:
    from bot.db.bookings import acquire_slot_lock, init_event_slots

    await init_event_slots(pool, "evt", 6)
    results = await asyncio.gather(
        acquire_slot_lock(pool, "evt", 2, 111),
        acquire_slot_lock(pool, "evt", 2, 222),
    )
    assert sorted(results) == [False, True]


async def test_plan_text_lives_and_dies_with_the_booking(pool) -> None:
    from bot.db.bookings import (
        acquire_slot_lock,
        admin_force_book_slot,
        get_plan_text,
        get_plan_texts,
        hand_back_slot,
        init_event_slots,
        release_slot_lock,
        set_plan_text,
    )

    await init_event_slots(pool, "evt", 6)
    assert await acquire_slot_lock(pool, "evt", 1, 111)
    await set_plan_text(pool, "evt", 1, 999, "Not the holder")  # ignored
    assert await get_plan_text(pool, "evt", 1) is None
    await set_plan_text(pool, "evt", 1, 111, "DJ One")
    assert await get_plan_texts(pool, ["evt"]) == {("evt", 1): "DJ One"}
    await admin_force_book_slot(pool, "evt", 1, 222)  # a new holder starts without text
    assert await get_plan_text(pool, "evt", 1) is None
    assert await hand_back_slot(pool, "evt", 1, 222, 111, plan_text="DJ One")
    assert await get_plan_text(pool, "evt", 1) == "DJ One"
    assert await release_slot_lock(pool, "evt", 1, 111)
    assert await get_plan_text(pool, "evt", 1) is None
    assert await get_plan_texts(pool, ["evt"]) == {}


async def test_booked_slots_query_and_release(pool) -> None:
    from bot.db.bookings import (
        acquire_slot_lock,
        get_booked_slots,
        init_event_slots,
        release_slot_lock,
    )

    await init_event_slots(pool, "a", 6)
    await init_event_slots(pool, "b", 6)
    assert await acquire_slot_lock(pool, "a", 3, 111)
    assert await acquire_slot_lock(pool, "b", 0, 222)
    assert await get_booked_slots(pool, ["a", "b", "c"]) == {"a": {3: 111}, "b": {0: 222}}
    await release_slot_lock(pool, "a", 3)
    assert await get_booked_slots(pool, ["a"]) == {}


async def test_profile_resubmission_updates_instead_of_failing(pool) -> None:
    from bot.db.djs import (
        get_dj_by_discord_id,
        get_notion_page_ids,
        insert_dj,
        set_notion_dj_page_id,
    )

    for name in ("First Name", "Second Name"):
        await insert_dj(pool, discord_user_id=7, dj_name=name, vrchat_name=None, logo_url=None,
                        stream_url_primary="https://example.com/a", stream_url_backup=None)
    row = await get_dj_by_discord_id(pool, 7)
    assert row["dj_name"] == "Second Name" and row["onboarding_complete"] is True
    await set_notion_dj_page_id(pool, 7, "page-7")
    assert await get_notion_page_ids(pool, [7, 8]) == {7: "page-7"}


async def test_dj_names_for_the_plan(pool) -> None:
    from bot.db.djs import get_dj_names, insert_dj

    for uid, name in ((7, "DJ Rayla"), (8, "Echo Fox")):
        await insert_dj(pool, discord_user_id=uid, dj_name=name, vrchat_name=None, logo_url=None,
                        stream_url_primary="https://example.com/a", stream_url_backup=None)
    assert await get_dj_names(pool, [7, 8, 9]) == {7: "DJ Rayla", 8: "Echo Fox"}
    assert await get_dj_names(pool, []) == {}


@pytest.mark.passive  # use the real bot.active, not the conftest stub
async def test_config_round_trip(pool) -> None:
    from bot import active
    from bot.db.config import get_value, set_value

    assert await active.is_active(pool, 42) is False
    await active.set_active(pool, 42, True)
    assert await active.is_active(pool, 42) is True
    await set_value(pool, 42, "timezone", "Europe/Berlin")
    assert await get_value(pool, 42, "timezone") == "Europe/Berlin"


async def test_release_frees_only_the_holders_own_slot(pool) -> None:
    from bot.db.bookings import (
        acquire_slot_lock,
        get_slot_lock_holder,
        init_event_slots,
        release_slot_lock,
    )

    await init_event_slots(pool, "evt", 6)
    assert await acquire_slot_lock(pool, "evt", 0, 111)
    assert await release_slot_lock(pool, "evt", 0, 222) is False  # stale button of another DJ
    assert await get_slot_lock_holder(pool, "evt", 0) == 111
    assert await release_slot_lock(pool, "evt", 0, 111) is True
    assert await release_slot_lock(pool, "evt", 0, 111) is False  # second tap frees nothing
    assert await release_slot_lock(pool, "evt", 5) is False       # never booked


async def test_holds_slot_in_event_counts_every_slot_of_the_dj(pool) -> None:
    from bot.db.bookings import (
        acquire_slot_lock,
        holds_slot_in_event,
        init_event_slots,
        release_slot_lock,
    )

    await init_event_slots(pool, "evt", 6)
    await init_event_slots(pool, "other", 6)
    assert await acquire_slot_lock(pool, "evt", 0, 111)
    assert await acquire_slot_lock(pool, "evt", 3, 111)
    assert await acquire_slot_lock(pool, "other", 1, 222)
    await release_slot_lock(pool, "evt", 0, 111)
    assert await holds_slot_in_event(pool, "evt", 111) is True
    await release_slot_lock(pool, "evt", 3, 111)
    assert await holds_slot_in_event(pool, "evt", 111) is False
    assert await holds_slot_in_event(pool, "evt", 222) is False


async def test_hand_back_only_while_the_forced_dj_still_holds_the_slot(pool) -> None:
    from bot.db.bookings import (
        acquire_slot_lock,
        admin_force_book_slot,
        get_slot_lock_holder,
        hand_back_slot,
        init_event_slots,
        release_slot_lock,
    )

    await init_event_slots(pool, "evt", 6)
    assert await acquire_slot_lock(pool, "evt", 0, 111)   # A
    await admin_force_book_slot(pool, "evt", 0, 222)      # admin gives it to B
    assert await hand_back_slot(pool, "evt", 0, 222, 111) is True
    assert await get_slot_lock_holder(pool, "evt", 0) == 111

    await admin_force_book_slot(pool, "evt", 0, 222)      # again, but meanwhile
    assert await release_slot_lock(pool, "evt", 0, 222)   # B cancels
    assert await acquire_slot_lock(pool, "evt", 0, 333)   # and C books
    assert await hand_back_slot(pool, "evt", 0, 222, 111) is False
    assert await get_slot_lock_holder(pool, "evt", 0) == 333
