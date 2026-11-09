"""Real-SQL checks of the repositories: locking, conflicts, status strings, migrations."""
import asyncio

import asyncpg
import pytest

from bot.db import bookings, djs, outreach, reminders, web_admins
from tests.pg import run_alembic

A, B = 111111111111111111, 222222222222222222


async def _dj(pool, user_id, name, active=True):
    await djs.insert_dj(
        pool, discord_user_id=user_id, dj_name=name, vrchat_name=None, logo_url=None,
        stream_url_primary="https://twitch.tv/x", stream_url_backup=None,
        active_booking=active,
    )


def test_migrations_go_down_and_up_again(schema) -> None:
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")


async def test_two_djs_racing_for_one_slot_get_exactly_one_lock(pool) -> None:
    results = await asyncio.gather(
        *(bookings.acquire_slot_lock(pool, "e1", 0, uid) for uid in (A, B, A + 1, B + 1))
    )
    assert results.count(True) == 1
    holder = await bookings.get_slot_lock_holder(pool, "e1", 0)
    assert holder in (A, B, A + 1, B + 1)


async def test_own_slot_counts_as_taken_and_release_needs_the_holder(pool) -> None:
    assert await bookings.acquire_slot_lock(pool, "e1", 1, A)
    assert not await bookings.release_slot_lock(pool, "e1", 1, expected_holder=B)
    assert await bookings.get_slot_lock_holder(pool, "e1", 1) == A
    assert await bookings.release_slot_lock(pool, "e1", 1, expected_holder=A)
    assert not await bookings.release_slot_lock(pool, "e1", 1, expected_holder=A)
    assert await bookings.acquire_slot_lock(pool, "e1", 1, B)


async def test_dj_slot_locks_are_ordered(pool) -> None:
    for event, index in (("e2", 1), ("e1", 0), ("e1", 2)):
        await bookings.admin_force_book_slot(pool, event, index, A)
    rows = await bookings.get_dj_slot_locks(pool, A)
    assert len(rows) == 3


async def test_status_strings_report_real_changes(pool) -> None:
    await _dj(pool, A, "Nova")
    assert await djs.update_active_booking(pool, discord_user_id=A, active_booking=False)
    assert not await djs.update_active_booking(pool, discord_user_id=B, active_booking=True)
    assert await djs.update_dj_fields(pool, A, dj_name="Nova 2")
    assert not await djs.update_dj_fields(pool, B, dj_name="Ghost")
    assert await web_admins.add_web_admin(pool, A)
    assert not await web_admins.add_web_admin(pool, A)
    assert await web_admins.remove_web_admin(pool, A)
    assert not await web_admins.remove_web_admin(pool, A)


async def test_dj_names_are_unique_after_normalizing(pool) -> None:
    await _dj(pool, A, "Moon  Beat")
    assert await djs.dj_name_exists(pool, " moon beat ")
    assert not await djs.dj_name_exists(pool, "moon beat", exclude_user_id=A)
    with pytest.raises(asyncpg.UniqueViolationError) as exc_info:
        await _dj(pool, B, "MOON BEAT")
    assert exc_info.value.constraint_name == "ux_djs_dj_name_norm"


async def test_guest_ids_count_down_and_are_never_reused(pool) -> None:
    first = await djs.next_guest_id(pool)
    await _dj(pool, first, "Guest One", active=False)
    assert first == -1
    assert await djs.delete_dj(pool, first)
    second = await djs.next_guest_id(pool)
    assert second == -2


async def test_delete_dj_removes_what_the_bot_keeps_about_them(pool) -> None:
    await _dj(pool, A, "Nova")
    await bookings.admin_force_book_slot(pool, "past", 0, A)
    await reminders.record_reminder_sent(pool, "past", A, 0)
    await outreach.upsert_sequence(pool, "past")
    await outreach.record_outreach_contact(pool, "past", A)
    assert await djs.delete_dj(pool, A)
    assert await bookings.get_slot_lock_holder(pool, "past", 0) is None
    assert not await reminders.has_reminder_been_sent(pool, "past", A, 0)
    assert await outreach.get_contacted_discord_ids(pool, "past") == []
    assert not await djs.delete_dj(pool, A)


async def test_outreach_pool_excludes_guests_and_inactive_djs(pool) -> None:
    await _dj(pool, A, "Active")
    await _dj(pool, B, "Inactive", active=False)
    await _dj(pool, -1, "Guest", active=True)
    ids = [row["discord_user_id"] for row in await djs.get_active_booking_djs(pool)]
    assert ids == [A]


async def test_waiting_invites_and_status_check(pool) -> None:
    await outreach.upsert_sequence(pool, "e1")
    await outreach.record_outreach_contact(pool, "e1", A)
    assert await outreach.get_waiting_discord_ids(pool, 12) == [A]
    await outreach.update_outreach_contact_status(pool, "e1", A, "declined")
    assert await outreach.get_waiting_discord_ids(pool, 12) == []
    with pytest.raises(asyncpg.CheckViolationError):
        await outreach.update_outreach_contact_status(pool, "e1", A, "bogus")


async def test_reminder_record_can_be_undone(pool) -> None:
    await reminders.record_reminder_sent(pool, "e1", A, 0)
    await reminders.record_reminder_sent(pool, "e1", A, 0)  # idempotent
    assert await reminders.has_reminder_been_sent(pool, "e1", A, 0)
    await reminders.forget_reminder_sent(pool, "e1", A, 0)
    assert not await reminders.has_reminder_been_sent(pool, "e1", A, 0)


async def test_logo_marked_synced_only_when_unchanged(pool) -> None:
    await djs.insert_dj(
        pool, discord_user_id=A, dj_name="Nova", vrchat_name=None, logo_url=None,
        stream_url_primary="https://t.tv/n", stream_url_backup=None, logo_image=b"one",
    )
    await djs.update_dj_fields(pool, A, logo_image=b"two")
    await djs.mark_logo_synced_if_unchanged(pool, A, b"one")
    assert await djs.list_unsynced_logo_user_ids(pool) == [A]
    await djs.mark_logo_synced_if_unchanged(pool, A, b"two")
    assert await djs.list_unsynced_logo_user_ids(pool) == []
