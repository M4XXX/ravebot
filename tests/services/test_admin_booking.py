"""Unit tests for bot/services/admin_booking.py — Story 6.3.

Tests cover: AdminBookingService.book_slot_override, cancel_slot_override.
All Notion, DB, and Discord API calls are mocked.

[Source: story 6-3-admin-booking-cancellation-override.md#Task 7]
"""
import datetime
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.notion.client import NotionWriteError, SlotRowMissingError
from bot.notion.models import EventInfo
from tests.notion.factories import make_event, make_slot

EVENT_PAGE_ID = "evt-page-001"


def _make_event(event_id: str = "2026-06-14 Rave Night #12") -> EventInfo:
    """Typed event; slot 1 is held by another DJ in Notion."""
    return make_event(
        event_id=event_id,
        page_id=EVENT_PAGE_ID,
        date=datetime.date(2026, 6, 14),
        start=datetime.time(20, 0),
        slot_minutes=60,
        slot_count=3,
        slots=[
            make_slot(1, dj_page_id="old-dj-page", dj_name="DJ Old", discord_id=999),
            make_slot(2),
            make_slot(3),
        ],
    )


EVENT_NAME = "2026-06-14 Rave Night #12"


def _make_service(**overrides):
    from bot.services.admin_booking import AdminBookingService

    bot = MagicMock()
    user = MagicMock()
    user.send = AsyncMock()
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)

    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(return_value=[_make_event()])
    notion_client.force_assign_slot = AsyncMock()
    notion_client.clear_slot = AsyncMock(return_value=True)

    log_service = MagicMock()
    for name in (
        "post_admin_override", "log_booked", "log_cancelled", "log_changed_by_organizer"
    ):
        setattr(log_service, name, AsyncMock())

    defaults = {
        "bot": bot,
        "db": AsyncMock(),
        "notion_client": notion_client,
        "log_service": log_service,
    }
    defaults.update(overrides)
    return AdminBookingService(**defaults)


def _patch_db(monkeypatch, *, holder=None, dj_page="dj-notion-id"):
    """Patch the Postgres helpers and ensure_dj_page; return (force, release, ensure)."""
    force = AsyncMock()
    release = AsyncMock()
    ensure = AsyncMock(return_value=dj_page)
    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", force)
    monkeypatch.setattr("bot.services.admin_booking.release_slot_lock", release)
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=holder)
    )
    monkeypatch.setattr("bot.services.admin_booking.ensure_dj_page", ensure)
    return force, release, ensure


# ---------------------------------------------------------------------------
# book_slot_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_book_slot_override_success(monkeypatch):
    """Happy path: event found by event_id, force-assigned in Notion, DM sent, logged."""
    svc = _make_service()
    force, release, ensure = _patch_db(monkeypatch)

    success, msg = await svc.book_slot_override(
        admin_name="Admin",
        dj_id=42,
        dj_name="DJ Scope",
        event_name=EVENT_NAME,
        slot_num=2,
    )

    assert success is True
    assert msg == ""
    force.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 1, 42)
    ensure.assert_awaited_once_with(svc.db, svc.notion_client, 42, "DJ Scope")
    event = svc.notion_client.list_events.return_value[0]
    svc.notion_client.force_assign_slot.assert_awaited_once_with(event, 1, "dj-notion-id")
    release.assert_not_awaited()
    dm = svc.bot.get_user.return_value.send.call_args[0][0]
    assert dm == f"An organizer booked you: {EVENT_NAME}, slot 2 at 21:00."
    svc.log_service.log_changed_by_organizer.assert_awaited_once_with(
        "Admin", "booked", "DJ Scope", event, 1
    )
    svc.log_service.log_booked.assert_not_awaited()
    svc.log_service.post_admin_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_matches_event_id_case_insensitive(monkeypatch):
    svc = _make_service()
    _patch_db(monkeypatch)

    success, _ = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X",
        event_name=EVENT_NAME.upper(), slot_num=1,
    )

    assert success is True
    svc.notion_client.force_assign_slot.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_slot_override_replaces_displaced_holder(monkeypatch):
    """Slot 1 is held by another DJ: force_assign_slot gets only the new DJ's page id."""
    svc = _make_service()
    force, release, _ = _patch_db(monkeypatch, holder=999, dj_page="new-dj-page")

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ New", event_name=EVENT_NAME, slot_num=1
    )

    assert success is True
    force.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, 42)
    args = svc.notion_client.force_assign_slot.call_args[0]
    assert args[1:] == (0, "new-dj-page")
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_event_not_found(monkeypatch):
    """Returns (False, error) when event name doesn't match any Notion event."""
    svc = _make_service()
    svc.notion_client.list_events = AsyncMock(return_value=[])

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Ghost Event", slot_num=1
    )

    assert success is False
    assert msg == "Event 'Ghost Event' not found in Notion."
    svc.notion_client.force_assign_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_list_events_fails(monkeypatch):
    svc = _make_service()
    svc.notion_client.list_events = AsyncMock(side_effect=RuntimeError("down"))

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Could not fetch events from Notion. Please try again in a moment."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [NotionWriteError("failed"), SlotRowMissingError("x", 1)]
)
async def test_book_slot_override_notion_write_fails(monkeypatch, error):
    """Returns (False, error) when the Notion write fails and releases the Postgres claim."""
    svc = _make_service()
    force, release, _ = _patch_db(monkeypatch)
    svc.notion_client.force_assign_slot = AsyncMock(side_effect=error)

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Notion write failed after 3 attempts. Please try again in a moment."
    force.assert_awaited_once()
    # slot lock must be undone
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)
    svc.log_service.log_changed_by_organizer.assert_not_awaited()  # no log on failure


@pytest.mark.asyncio
@pytest.mark.parametrize("ensure_result", [None, RuntimeError("boom")])
async def test_book_slot_override_no_dj_page_fails_and_releases(monkeypatch, ensure_result):
    """ensure_dj_page returning None (or raising) takes the Notion failure path."""
    svc = _make_service()
    force, release, ensure = _patch_db(monkeypatch)
    if isinstance(ensure_result, Exception):
        ensure.side_effect = ensure_result
    else:
        ensure.return_value = ensure_result

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Notion write failed after 3 attempts. Please try again in a moment."
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)
    svc.notion_client.force_assign_slot.assert_not_awaited()
    svc.log_service.log_changed_by_organizer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["force_assign", "no_dj_page", "slot_row_missing"]
)
async def test_book_slot_override_failure_restores_previous_holder(monkeypatch, failure):
    """DJ 999 held the slot; booking DJ 42 fails in Notion -> Postgres holder restored to 999."""
    svc = _make_service()
    force, release, ensure = _patch_db(monkeypatch, holder=999)
    if failure == "force_assign":
        svc.notion_client.force_assign_slot = AsyncMock(side_effect=NotionWriteError("x"))
    elif failure == "no_dj_page":
        ensure.return_value = None
    else:
        svc.notion_client.force_assign_slot = AsyncMock(
            side_effect=SlotRowMissingError("x", 1)
        )

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Notion write failed after 3 attempts. Please try again in a moment."
    assert force.await_args_list == [
        ((svc.db, EVENT_PAGE_ID, 0, 42),),
        ((svc.db, EVENT_PAGE_ID, 0, 999),),
    ]
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_failure_same_dj_previous_holder_releases(monkeypatch):
    """Previous holder is the booked DJ itself: release as before, no restore."""
    svc = _make_service()
    force, release, _ = _patch_db(monkeypatch, holder=42)
    svc.notion_client.force_assign_slot = AsyncMock(side_effect=NotionWriteError("x"))

    success, _ = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    force.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, 42)
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)


@pytest.mark.asyncio
async def test_book_slot_override_restore_failure_is_logged(monkeypatch, caplog):
    """The restore failing is logged; the method still returns the failure tuple."""
    import logging

    svc = _make_service()
    force, release, _ = _patch_db(monkeypatch, holder=999)
    force.side_effect = [None, RuntimeError("db down")]
    svc.notion_client.force_assign_slot = AsyncMock(side_effect=NotionWriteError("x"))

    with caplog.at_level(logging.ERROR):
        success, msg = await svc.book_slot_override(
            admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
        )

    assert (success, msg) == (
        False, "Notion write failed after 3 attempts. Please try again in a moment."
    )
    assert force.await_count == 2
    release.assert_not_awaited()
    assert any("restore" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_book_slot_override_holder_read_fails_falls_back_to_release(monkeypatch):
    svc = _make_service()
    _, release, _ = _patch_db(monkeypatch)
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder",
        AsyncMock(side_effect=RuntimeError("db")),
    )
    svc.notion_client.force_assign_slot = AsyncMock(side_effect=NotionWriteError("x"))

    success, _ = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)


@pytest.mark.asyncio
async def test_cancel_slot_override_notion_held_by_other_logs_reconcile(monkeypatch, caplog):
    import logging

    svc = _make_service()
    _patch_db(monkeypatch, holder=42)
    svc.notion_client.clear_slot = AsyncMock(return_value=False)

    with caplog.at_level(logging.WARNING):
        success, _ = await svc.cancel_slot_override(
            admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
        )

    assert success is True
    assert any(
        "Notion slot held by another DJ; sync will reconcile" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_book_slot_override_db_error(monkeypatch):
    svc = _make_service()
    force, _, ensure = _patch_db(monkeypatch)
    force.side_effect = RuntimeError("db down")

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Database error while writing the slot lock. Please try again in a moment."
    ensure.assert_not_awaited()
    svc.notion_client.force_assign_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_dm_forbidden_does_not_fail(monkeypatch):
    """DM failure (Forbidden) is non-fatal — still returns (True, '')."""
    svc = _make_service()
    _patch_db(monkeypatch)

    # Make DM fail with Forbidden
    blocked_user = MagicMock()
    blocked_user.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(), "DMs blocked"))
    svc.bot.get_user = MagicMock(return_value=blocked_user)

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ Lock", event_name=EVENT_NAME, slot_num=1
    )

    assert success is True
    assert msg == ""
    svc.log_service.log_changed_by_organizer.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_slot_override_no_notion_client():
    """Returns (False, error) immediately when notion_client is None."""
    svc = _make_service(notion_client=None)

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Event", slot_num=1
    )

    assert success is False
    assert "notion" in msg.lower()


# ---------------------------------------------------------------------------
# cancel_slot_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_slot_override_success(monkeypatch):
    """Happy path: slot held by correct DJ, Notion slot cleared, lock released, DM sent."""
    svc = _make_service()
    _, release, ensure = _patch_db(monkeypatch, holder=42)

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin",
        dj_id=42,
        dj_name="DJ Scope",
        event_name=EVENT_NAME,
        slot_num=2,
    )

    assert success is True
    assert msg == ""
    ensure.assert_awaited_once_with(svc.db, svc.notion_client, 42, "DJ Scope")
    event = svc.notion_client.list_events.return_value[0]
    svc.notion_client.clear_slot.assert_awaited_once_with(event, 1, "dj-notion-id")
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 1, expected_holder=42)
    dm = svc.bot.get_user.return_value.send.call_args[0][0]
    assert dm == (
        f"An organizer cancelled your booking: {EVENT_NAME}, slot 2 at 21:00."
    )
    svc.log_service.log_changed_by_organizer.assert_awaited_once_with(
        "Admin", "removed", "DJ Scope", event, 1
    )
    svc.log_service.log_cancelled.assert_not_awaited()
    svc.log_service.post_admin_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_slot_not_held(monkeypatch):
    """Returns (False, 'No booking found') when holder is None."""
    svc = _make_service()
    _, release, _ = _patch_db(monkeypatch, holder=None)

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "No booking found for @DJ X in that slot."
    svc.notion_client.clear_slot.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_slot_held_by_different_dj(monkeypatch):
    """Returns (False, 'No booking found') when slot is held by a different DJ."""
    svc = _make_service()
    _patch_db(monkeypatch, holder=999)

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert "no booking found" in msg.lower()
    svc.notion_client.clear_slot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [NotionWriteError("failed"), SlotRowMissingError("x", 1)]
)
async def test_cancel_slot_override_notion_error_still_releases_lock(monkeypatch, error):
    """Slot lock is always released even when the Notion clear fails."""
    svc = _make_service()
    _, release, _ = _patch_db(monkeypatch, holder=42)
    svc.notion_client.clear_slot = AsyncMock(side_effect=error)

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == (
        "Notion write failed. The slot lock is released, but Notion may be out of sync. "
        "Check the slot in Notion."
    )
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)  # MUST be freed
    svc.log_service.log_changed_by_organizer.assert_not_awaited()  # no log on failure


@pytest.mark.asyncio
@pytest.mark.parametrize("ensure_result", [None, RuntimeError("boom")])
async def test_cancel_slot_override_no_dj_page_still_releases_lock(monkeypatch, ensure_result):
    svc = _make_service()
    _, release, ensure = _patch_db(monkeypatch, holder=42)
    if isinstance(ensure_result, Exception):
        ensure.side_effect = ensure_result
    else:
        ensure.return_value = ensure_result

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == (
        "Notion write failed. The slot lock is released, but Notion may be out of sync. "
        "Check the slot in Notion."
    )
    svc.notion_client.clear_slot.assert_not_awaited()
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)


@pytest.mark.asyncio
async def test_cancel_slot_override_notion_row_held_by_other_dj_still_succeeds(monkeypatch):
    """clear_slot False (Notion row holds only other DJs): lock released, cancel succeeds."""
    svc = _make_service()
    _, release, _ = _patch_db(monkeypatch, holder=42)
    svc.notion_client.clear_slot = AsyncMock(return_value=False)

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is True
    assert msg == ""
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)


@pytest.mark.asyncio
async def test_cancel_slot_override_event_not_found(monkeypatch):
    """Returns (False, error) when event name doesn't match."""
    svc = _make_service()
    svc.notion_client.list_events = AsyncMock(return_value=[])

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Ghost Event", slot_num=1
    )

    assert success is False
    assert "not found" in msg.lower()


# ---------------------------------------------------------------------------
# set_dj_active_status
# ---------------------------------------------------------------------------


def _make_role_chain(svc, add_raises=None, remove_raises=None):
    """Wire up bot.get_guild → guild.get_member → guild.roles for role op tests."""
    mock_role = MagicMock()
    mock_member = MagicMock()
    mock_member.add_roles = AsyncMock(side_effect=add_raises)
    mock_member.remove_roles = AsyncMock(side_effect=remove_raises)
    mock_guild = MagicMock()
    mock_guild.get_member = MagicMock(return_value=mock_member)
    svc.bot.get_guild = MagicMock(return_value=mock_guild)
    return mock_guild, mock_member, mock_role


@pytest.mark.asyncio
async def test_set_dj_active_status_activates_dj(monkeypatch):
    """Happy path: active=True updates DB, assigns role, returns (True, '')."""
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.update_active_booking", AsyncMock(return_value=True)
    )
    mock_guild, mock_member, mock_role = _make_role_chain(svc)
    monkeypatch.setattr(disnake.utils, "get", MagicMock(return_value=mock_role))

    success, msg = await svc.set_dj_active_status(111, 42, "DJ Scope", True)

    assert success is True
    assert msg == ""
    mock_member.add_roles.assert_awaited_once_with(
        mock_role, reason="Admin override via /admin dj set-active"
    )
    mock_member.remove_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_dj_active_status_deactivates_dj(monkeypatch):
    """Happy path: active=False updates DB, removes role, returns (True, '')."""
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.update_active_booking", AsyncMock(return_value=True)
    )
    mock_guild, mock_member, mock_role = _make_role_chain(svc)
    monkeypatch.setattr(disnake.utils, "get", MagicMock(return_value=mock_role))

    success, msg = await svc.set_dj_active_status(111, 42, "DJ Scope", False)

    assert success is True
    assert msg == ""
    mock_member.remove_roles.assert_awaited_once_with(
        mock_role, reason="Admin override via /admin dj set-active"
    )
    mock_member.add_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_dj_active_status_dj_not_in_db(monkeypatch):
    """Returns (False, 'not registered') when update_active_booking returns False."""
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.update_active_booking", AsyncMock(return_value=False)
    )

    success, msg = await svc.set_dj_active_status(111, 42, "DJ Ghost", True)

    assert success is False
    assert "not registered" in msg.lower()


@pytest.mark.asyncio
async def test_set_dj_active_status_role_forbidden_nonfatal(monkeypatch):
    """Forbidden on add_roles is non-fatal — still returns (True, '')."""
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.update_active_booking", AsyncMock(return_value=True)
    )
    mock_guild, mock_member, mock_role = _make_role_chain(
        svc, add_raises=disnake.Forbidden(MagicMock(), "Missing permissions")
    )
    monkeypatch.setattr(disnake.utils, "get", MagicMock(return_value=mock_role))

    success, msg = await svc.set_dj_active_status(111, 42, "DJ Locked", True)

    assert success is True
    assert msg == ""


@pytest.mark.asyncio
async def test_set_dj_active_status_role_http_exception_nonfatal(monkeypatch):
    """HTTPException on remove_roles is non-fatal — still returns (True, '')."""
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.update_active_booking", AsyncMock(return_value=True)
    )
    mock_guild, mock_member, mock_role = _make_role_chain(
        svc, remove_raises=disnake.HTTPException(MagicMock(), "Unknown error")
    )
    monkeypatch.setattr(disnake.utils, "get", MagicMock(return_value=mock_role))

    success, msg = await svc.set_dj_active_status(111, 42, "DJ Net", False)

    assert success is True
    assert msg == ""


@pytest.mark.asyncio
async def test_admin_book_and_cancel_work_without_log_service(monkeypatch):
    svc = _make_service(log_service=None)
    _patch_db(monkeypatch, holder=42)
    ok, _ = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ Scope", event_name=EVENT_NAME, slot_num=2
    )
    assert ok is True
    ok, _ = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ Scope", event_name=EVENT_NAME, slot_num=2
    )
    assert ok is True
