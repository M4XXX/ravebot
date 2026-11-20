"""Unit tests for bot/services/admin_booking.py — Story 6.3.

Tests cover: AdminBookingService.book_slot_override, cancel_slot_override,
set_dj_active_status. Postgres helpers and Discord API calls are mocked; the
admin overrides never call Notion (the mirror worker follows via changed()).

[Source: story 6-3-admin-booking-cancellation-override.md#Task 7]
"""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.notion.models import EventInfo
from bot.ui import texts
from tests.fake_events import fake_events
from tests.notion.factories import make_event, make_slot

EVENT_PAGE_ID = "evt-page-001"


def _make_event(event_id: str = "2026-06-14 Rave Night #12") -> EventInfo:
    """Typed event; slot 1 is held by another DJ."""
    return make_event(
        event_id=event_id,
        page_id=EVENT_PAGE_ID,
        date=datetime.date(2026, 6, 14),
        start=datetime.time(20, 0),
        slot_minutes=60,
        slot_count=3,
        slots=[
            make_slot(1, dj_page_id="999", dj_name="DJ Old", discord_id=999),
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

    fake_events.list_events = AsyncMock(return_value=[_make_event()])

    log_service = MagicMock()
    for name in (
        "log_booked", "log_cancelled", "log_changed_by_organizer", "log_fully_booked", "post_error"
    ):
        setattr(log_service, name, AsyncMock())

    defaults = {
        "bot": bot,
        "db": AsyncMock(),
        "log_service": log_service,
        "ctx": SimpleNamespace(mirror=None),
    }
    defaults.update(overrides)
    return AdminBookingService(**defaults)


def _patch_db(monkeypatch, *, holder=None, released=True, registered=True):
    """Patch the Postgres helpers and changed(); return (force, release, changed).

    ``holder``: previous holder returned by force_slot_holder. ``released``: what
    release_slot_lock returns (False: the DJ does not hold the slot).
    """
    force = AsyncMock(return_value=holder)
    release = AsyncMock(return_value=released)
    queued = MagicMock()
    monkeypatch.setattr("bot.services.admin_booking.force_slot_holder", force)
    monkeypatch.setattr("bot.services.admin_booking.release_slot_lock", release)
    monkeypatch.setattr(
        "bot.services.admin_booking.get_dj_by_discord_id",
        AsyncMock(return_value={"dj_name": "x"} if registered else None),
    )
    monkeypatch.setattr("bot.services.admin_booking.changed", queued)
    return force, release, queued


# ---------------------------------------------------------------------------
# book_slot_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_book_slot_override_success(monkeypatch):
    """Happy path: event found by event_id, holder set in Postgres, queued, DM sent, logged."""
    svc = _make_service()
    force, release, queued = _patch_db(monkeypatch)

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
    queued.assert_called_once_with(svc._ctx, "slot", f"{EVENT_PAGE_ID}:1")
    release.assert_not_awaited()
    event = fake_events.list_events.return_value[0]
    dm = svc.bot.get_user.return_value.send.call_args[0][0]
    assert dm == f"An organizer booked you: {EVENT_NAME}, slot 2 at 21:00."
    svc.log_service.log_changed_by_organizer.assert_awaited_once_with(
        "Admin", "booked", "DJ Scope", event, 1
    )
    svc.log_service.log_booked.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_matches_event_id_case_insensitive(monkeypatch):
    svc = _make_service()
    force, _, _ = _patch_db(monkeypatch)

    success, _ = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X",
        event_name=EVENT_NAME.upper(), slot_num=1,
    )

    assert success is True
    force.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_slot_override_replaces_displaced_holder(monkeypatch):
    """Slot 1 was held by DJ 999: the new DJ holds it, 999 gets a cancellation DM."""
    svc = _make_service()
    force, release, _ = _patch_db(monkeypatch, holder=999)

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ New", event_name=EVENT_NAME, slot_num=1
    )

    assert success is True
    force.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, 42)
    release.assert_not_awaited()
    sent_to = [c.args[0] for c in svc.bot.get_user.call_args_list]
    assert 999 in sent_to
    dms = [c.args[0] for c in svc.bot.get_user.return_value.send.call_args_list]
    assert f"An organizer cancelled your booking: {EVENT_NAME}, slot 1 at 20:00." in dms


@pytest.mark.asyncio
async def test_book_slot_override_event_not_found(monkeypatch):
    """Returns (False, error) when event name doesn't match any event."""
    svc = _make_service()
    force, _, queued = _patch_db(monkeypatch)
    fake_events.list_events = AsyncMock(return_value=[])

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Ghost Event", slot_num=1
    )

    assert success is False
    assert msg == "Event 'Ghost Event' not found."
    force.assert_not_awaited()
    queued.assert_not_called()


@pytest.mark.asyncio
async def test_book_slot_override_list_events_fails(monkeypatch):
    svc = _make_service()
    fake_events.list_events = AsyncMock(side_effect=RuntimeError("down"))

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Could not read the events. Please try again in a moment."


@pytest.mark.asyncio
async def test_book_slot_override_slot_out_of_range(monkeypatch):
    svc = _make_service()
    force, _, _ = _patch_db(monkeypatch)

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=4
    )

    assert (success, msg) == (False, f"'{EVENT_NAME}' has slots 1 to 3.")
    force.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_unregistered_dj_fails(monkeypatch):
    """A DJ without a djs row cannot hold a slot; nothing is written."""
    svc = _make_service()
    force, _, queued = _patch_db(monkeypatch, registered=False)

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ Ghost", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == (
        "@DJ Ghost is not registered in the system. They may need to complete onboarding."
    )
    force.assert_not_awaited()
    queued.assert_not_called()
    svc.log_service.log_changed_by_organizer.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_db_error(monkeypatch):
    svc = _make_service()
    force, _, queued = _patch_db(monkeypatch)
    force.side_effect = RuntimeError("db down")

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Database error while writing the slot. Please try again in a moment."
    queued.assert_not_called()
    svc.log_service.log_changed_by_organizer.assert_not_awaited()  # no log on failure


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
async def test_book_and_cancel_work_without_notion(monkeypatch):
    """Notion off (ctx.mirror None, real changed()): the overrides still work."""
    svc = _make_service()
    _patch_db(monkeypatch)
    from bot.sync.worker import changed

    monkeypatch.setattr("bot.services.admin_booking.changed", changed)

    ok, _ = await svc.book_slot_override("Admin", 42, "DJ X", EVENT_NAME, 2)
    assert ok is True
    ok, _ = await svc.cancel_slot_override("Admin", 42, "DJ X", EVENT_NAME, 2)
    assert ok is True


# ---------------------------------------------------------------------------
# cancel_slot_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_slot_override_success(monkeypatch):
    """Happy path: slot held by the DJ, lock released, queued, DM sent, logged."""
    svc = _make_service()
    _, release, queued = _patch_db(monkeypatch)

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin",
        dj_id=42,
        dj_name="DJ Scope",
        event_name=EVENT_NAME,
        slot_num=2,
    )

    assert success is True
    assert msg == ""
    event = fake_events.list_events.return_value[0]
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 1, expected_holder=42)
    queued.assert_called_once_with(svc._ctx, "slot", f"{EVENT_PAGE_ID}:1")
    dm = svc.bot.get_user.return_value.send.call_args[0][0]
    assert dm == (
        f"An organizer cancelled your booking: {EVENT_NAME}, slot 2 at 21:00."
    )
    svc.log_service.log_changed_by_organizer.assert_awaited_once_with(
        "Admin", "removed", "DJ Scope", event, 1
    )
    svc.log_service.log_cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_slot_not_held(monkeypatch):
    """Returns (False, 'No booking found') when the DJ does not hold the slot."""
    svc = _make_service()
    _, release, queued = _patch_db(monkeypatch, released=False)

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "No booking found for @DJ X in that slot."
    release.assert_awaited_once_with(svc.db, EVENT_PAGE_ID, 0, expected_holder=42)
    queued.assert_not_called()
    svc.bot.get_user.return_value.send.assert_not_awaited()
    svc.log_service.log_changed_by_organizer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_db_error(monkeypatch):
    svc = _make_service()
    _, release, queued = _patch_db(monkeypatch)
    release.side_effect = RuntimeError("db down")

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name=EVENT_NAME, slot_num=1
    )

    assert success is False
    assert msg == "Database error while writing the slot. Please try again in a moment."
    queued.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_slot_override_event_not_found(monkeypatch):
    """Returns (False, error) when event name doesn't match."""
    svc = _make_service()
    fake_events.list_events = AsyncMock(return_value=[])

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
        mock_role, reason="Admin override via /automatic dj booking"
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
        mock_role, reason="Admin override via /automatic dj booking"
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


@pytest.mark.asyncio
async def test_organizer_booking_last_slot_logs_fully_booked(monkeypatch):
    from tests.services.test_fully_booked import FakeStore

    FakeStore().patch(monkeypatch)
    _patch_db(monkeypatch)
    full = make_event(
        event_id=EVENT_NAME,
        page_id=EVENT_PAGE_ID,
        slot_count=2,
        slots=[make_slot(1, dj_page_id="a"), make_slot(2, dj_page_id="b")],
    )
    svc = _make_service()
    fake_events.list_events = AsyncMock(side_effect=[[_make_event()], [full]])
    ok, _ = await svc.book_slot_override("Admin", 42, "DJ Scope", EVENT_NAME, 2)
    assert ok is True
    svc.log_service.log_fully_booked.assert_awaited_once_with(full)
    svc.log_service.log_changed_by_organizer.assert_awaited_once()


@pytest.mark.asyncio
async def test_organizer_cancellation_does_not_announce_open_event(monkeypatch):
    from tests.services.test_fully_booked import FakeStore

    FakeStore().patch(monkeypatch)
    _patch_db(monkeypatch, holder=42)
    svc = _make_service()
    ok, _ = await svc.cancel_slot_override("Admin", 42, "DJ Scope", EVENT_NAME, 1)
    svc.log_service.log_fully_booked.assert_not_awaited()


def _block_dms(svc):
    blocked = MagicMock()
    blocked.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(), "DMs blocked"))
    svc.bot.get_user = MagicMock(return_value=blocked)


@pytest.mark.asyncio
async def test_organizer_booking_undelivered_dm_posts_entry(monkeypatch):
    svc = _make_service()
    _patch_db(monkeypatch)
    _block_dms(svc)
    ok, _ = await svc.book_slot_override("Admin", 42, "DJ Lock", EVENT_NAME, 2)
    assert ok is True
    kwargs = svc.log_service.post_error.await_args.kwargs
    assert kwargs["error_code"].name == "ERR_DM_FAILED"
    assert "DJ Lock" in kwargs["trigger"]
    assert kwargs["manual_action"] == texts.dm_failed_todo("DJ Lock")


@pytest.mark.asyncio
async def test_organizer_cancel_undelivered_dm_posts_entry(monkeypatch):
    svc = _make_service()
    _patch_db(monkeypatch, holder=42)
    _block_dms(svc)
    ok, _ = await svc.cancel_slot_override("Admin", 42, "DJ Lock", EVENT_NAME, 1)
    assert ok is True
    kwargs = svc.log_service.post_error.await_args.kwargs
    assert kwargs["error_code"].name == "ERR_DM_FAILED"
    assert kwargs["manual_action"] == texts.dm_failed_todo("DJ Lock")


@pytest.mark.asyncio
async def test_delivered_dm_posts_no_error_entry(monkeypatch):
    svc = _make_service()
    _patch_db(monkeypatch)
    await svc.book_slot_override("Admin", 42, "DJ Lock", EVENT_NAME, 2)
    svc.log_service.post_error.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_set_dj_active_status_uses_stored_invites_role(monkeypatch, active):
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.update_active_booking", AsyncMock(return_value=True)
    )
    mock_guild, mock_member, _ = _make_role_chain(svc)
    mock_guild.id = 111
    old, stored = MagicMock(), MagicMock()
    old.name = "Active Booking"
    old.id = 1
    stored.name = "Invites"
    stored.id = 5
    mock_guild.roles = [old, stored]
    mock_guild.get_role = lambda rid: stored if rid == 5 else None
    monkeypatch.setattr("bot.services.roles.get_value", AsyncMock(return_value="5"))

    success, _ = await svc.set_dj_active_status(111, 42, "DJ Scope", active)

    assert success is True
    op = mock_member.add_roles if active else mock_member.remove_roles
    assert op.await_args[0][0] is stored


@pytest.mark.asyncio
async def test_set_dj_active_status_deleted_stored_role_is_warning_only(monkeypatch):
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.update_active_booking", AsyncMock(return_value=True)
    )
    mock_guild, mock_member, _ = _make_role_chain(svc)
    mock_guild.id = 111
    mock_guild.roles = []
    mock_guild.get_role = lambda rid: None
    monkeypatch.setattr("bot.services.roles.get_value", AsyncMock(return_value="5"))

    assert await svc.set_dj_active_status(111, 42, "DJ Scope", True) == (True, "")
    mock_member.add_roles.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", [0, 4])
async def test_slot_number_outside_the_event_is_rejected(monkeypatch, slot) -> None:
    force, release, _ = _patch_db(monkeypatch)
    service = _make_service()
    ok, msg = await service.book_slot_override("admin", 42, "DJ X", EVENT_NAME, slot)
    assert not ok and "slots 1 to 3" in msg
    ok, msg = await service.cancel_slot_override("admin", 42, "DJ X", EVENT_NAME, slot)
    assert not ok and "slots 1 to 3" in msg
    force.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_book_tells_the_displaced_dj(monkeypatch) -> None:
    _patch_db(monkeypatch, holder=999)
    monkeypatch.setattr("bot.services.admin_booking.recheck_fully_booked", AsyncMock())
    service = _make_service()
    sent: list[tuple[int, str]] = []

    async def fake_dm(bot, user_id, text):
        sent.append((user_id, text))
        return True

    monkeypatch.setattr("bot.services.admin_booking.dm_user", fake_dm)
    ok, _ = await service.book_slot_override("admin", 42, "DJ X", EVENT_NAME, 1)
    assert ok
    assert [uid for uid, _ in sent] == [42, 999]
    assert sent[1][1] == texts.dm_cancelled(_make_event(), 0)
