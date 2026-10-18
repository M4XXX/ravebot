"""Unit tests for bot/services/admin_booking.py — Story 6.3.

Tests cover: AdminBookingService.book_slot_override, cancel_slot_override.
All Notion, DB, and Discord API calls are mocked.

[Source: story 6-3-admin-booking-cancellation-override.md#Task 7]
"""
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.notion.client import NotionWriteError


def _make_event(name: str = "Rave Night #12", event_id: str = "evt-001") -> dict:
    """Build a minimal Notion event page object."""
    return {
        "id": event_id,
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Date": {"date": {"start": "2026-06-14T20:00:00+00:00"}},
            "DJs": {"relation": [{"id": "dj-page-1"}]},
        },
    }


def _make_service(**overrides):
    from bot.services.admin_booking import AdminBookingService

    bot = MagicMock()
    user = MagicMock()
    user.send = AsyncMock()
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)

    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(return_value=[_make_event()])
    notion_client.write_booking = AsyncMock()
    notion_client.delete_booking = AsyncMock()

    log_service = MagicMock()
    log_service.post_admin_override = AsyncMock()

    defaults = {
        "bot": bot,
        "db": AsyncMock(),
        "notion_client": notion_client,
        "log_service": log_service,
    }
    defaults.update(overrides)
    return AdminBookingService(**defaults)


# ---------------------------------------------------------------------------
# book_slot_override — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_book_slot_override_success(monkeypatch):
    """Happy path: event found, notion write succeeds, DM sent, override logged."""
    svc = _make_service()

    monkeypatch.setattr(
        "bot.services.admin_booking.admin_force_book_slot", AsyncMock()
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id",
        AsyncMock(return_value="dj-notion-id"),
    )

    success, msg = await svc.book_slot_override(
        admin_name="Admin",
        dj_id=42,
        dj_name="DJ Scope",
        event_name="Rave Night #12",
        slot_num=1,
    )

    assert success is True
    assert msg == ""
    svc.notion_client.write_booking.assert_awaited_once()
    svc.log_service.post_admin_override.assert_awaited_once()
    call = svc.log_service.post_admin_override.call_args[0][0]
    assert "[ADMIN OVERRIDE]" in call
    assert "Admin" in call
    assert "booked" in call
    assert "DJ Scope" in call
    assert "Rave Night #12" in call


@pytest.mark.asyncio
async def test_book_slot_override_event_not_found(monkeypatch):
    """Returns (False, error) when event name doesn't match any Notion event."""
    svc = _make_service()
    svc.notion_client.poll_events = AsyncMock(return_value=[])

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Ghost Event", slot_num=1
    )

    assert success is False
    assert "not found" in msg.lower()
    svc.notion_client.write_booking.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_notion_write_fails(monkeypatch):
    """Returns (False, error) on NotionWriteError and releases slot lock."""
    svc = _make_service()

    force_book_mock = AsyncMock()
    release_mock = AsyncMock()
    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", force_book_mock)
    monkeypatch.setattr("bot.services.admin_booking.release_slot_lock", release_mock)
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id", AsyncMock(return_value=None)
    )
    svc.notion_client.write_booking = AsyncMock(side_effect=NotionWriteError("failed"))

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is False
    assert "notion write failed" in msg.lower()
    release_mock.assert_awaited_once_with(svc.db, "evt-001", 0, 42)  # slot lock must be undone
    svc.log_service.post_admin_override.assert_not_awaited()  # no log on failure


@pytest.mark.asyncio
async def test_book_slot_override_notion_failure_gives_slot_back_to_displaced_dj(monkeypatch):
    """A failed force-book over an occupied slot restores the previous holder."""
    svc = _make_service()

    hand_back = AsyncMock(return_value=True)
    release_mock = AsyncMock()
    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", AsyncMock())
    monkeypatch.setattr("bot.services.admin_booking.hand_back_slot", hand_back)
    monkeypatch.setattr("bot.services.admin_booking.release_slot_lock", release_mock)
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=999)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id", AsyncMock(return_value=None)
    )
    svc.notion_client.write_booking = AsyncMock(side_effect=NotionWriteError("failed"))

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is False
    assert "try again" in msg
    hand_back.assert_awaited_once_with(svc.db, "evt-001", 0, 42, 999)
    release_mock.assert_not_awaited()
    svc.notion_client.delete_booking.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_rollback_tells_admin_when_the_slot_changed_hands(monkeypatch):
    """The slot was cancelled and rebooked during the Notion retries: nobody loses it."""
    svc = _make_service()
    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", AsyncMock())
    monkeypatch.setattr(
        "bot.services.admin_booking.hand_back_slot", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=999)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id", AsyncMock(return_value=None)
    )
    svc.notion_client.write_booking = AsyncMock(side_effect=NotionWriteError("failed"))

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is False
    assert "could not be given back" in msg


@pytest.mark.asyncio
async def test_book_slot_override_dm_forbidden_does_not_fail(monkeypatch):
    """DM failure (Forbidden) is non-fatal — still returns (True, '')."""
    svc = _make_service()

    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", AsyncMock())
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id", AsyncMock(return_value=None)
    )

    # Make DM fail with Forbidden
    blocked_user = MagicMock()
    blocked_user.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(), "DMs blocked"))
    svc.bot.get_user = MagicMock(return_value=blocked_user)

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ Lock", event_name="Rave Night #12", slot_num=1
    )

    assert success is True
    assert msg == ""
    svc.log_service.post_admin_override.assert_awaited_once()


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
# cancel_slot_override — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_slot_override_success(monkeypatch):
    """Happy path: slot held by correct DJ, Notion delete succeeds, DM sent."""
    svc = _make_service()

    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=42)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id",
        AsyncMock(return_value="dj-notion-id"),
    )
    release_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.services.admin_booking.release_slot_lock", release_mock)
    monkeypatch.setattr(
        "bot.services.admin_booking.holds_slot_in_event", AsyncMock(return_value=False)
    )

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin",
        dj_id=42,
        dj_name="DJ Scope",
        event_name="Rave Night #12",
        slot_num=1,
    )

    assert success is True
    assert msg == ""
    svc.notion_client.delete_booking.assert_awaited_once()
    assert svc.notion_client.delete_booking.call_args.args[1] == "dj-notion-id"
    release_mock.assert_awaited_once_with(svc.db, "evt-001", 0, 42)
    svc.log_service.post_admin_override.assert_awaited_once()
    call = svc.log_service.post_admin_override.call_args[0][0]
    assert "[ADMIN OVERRIDE]" in call
    assert "cancelled" in call
    assert "from" in call
    assert "DJ Scope" in call
    assert "Rave Night #12" in call


@pytest.mark.asyncio
async def test_cancel_slot_override_slot_not_held(monkeypatch):
    """Returns (False, 'No booking found') when holder is None."""
    svc = _make_service()

    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=None)
    )

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is False
    assert "no booking found" in msg.lower()
    svc.notion_client.delete_booking.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_slot_held_by_different_dj(monkeypatch):
    """Returns (False, 'No booking found') when slot is held by a different DJ."""
    svc = _make_service()

    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=999)
    )

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is False
    assert "no booking found" in msg.lower()
    svc.notion_client.delete_booking.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_notion_error_still_releases_lock(monkeypatch):
    """Slot lock is always released even when Notion delete_booking raises."""
    svc = _make_service()

    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=42)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id", AsyncMock(return_value=None)
    )
    release_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.services.admin_booking.release_slot_lock", release_mock)
    monkeypatch.setattr(
        "bot.services.admin_booking.holds_slot_in_event", AsyncMock(return_value=False)
    )
    svc.notion_client.delete_booking = AsyncMock(side_effect=NotionWriteError("failed"))

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is False
    assert "notion" in msg.lower() and "released" in msg.lower()
    release_mock.assert_awaited_once()  # slot lock MUST be freed even on Notion error
    svc.bot.get_user.return_value.send.assert_awaited_once()  # the DJ still hears about it
    svc.log_service.post_admin_override.assert_awaited_once()  # the cancel did happen


@pytest.mark.asyncio
async def test_cancel_slot_override_keeps_notion_link_while_dj_holds_another_slot(monkeypatch):
    """The DJs relation lists a DJ once per event: it stays while another slot is held."""
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=42)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.release_slot_lock", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.holds_slot_in_event", AsyncMock(return_value=True)
    )

    success, _ = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is True
    svc.notion_client.delete_booking.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_slot_changed_hands_before_release(monkeypatch):
    """The slot was rebooked between the check and the release: nothing is touched."""
    svc = _make_service()
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=42)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.release_slot_lock", AsyncMock(return_value=False)
    )

    success, msg = await svc.cancel_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ X", event_name="Rave Night #12", slot_num=1
    )

    assert success is False
    assert "no booking found" in msg.lower()
    svc.notion_client.delete_booking.assert_not_awaited()
    svc.log_service.post_admin_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_slot_override_removes_displaced_dj_from_notion(monkeypatch):
    """Force-booking over an occupied slot removes the old DJ's Notion ID from the relation."""
    svc = _make_service()
    svc.notion_client.poll_events = AsyncMock(
        return_value=[{
            "id": "evt-001",
            "properties": {
                "Name": {"title": [{"plain_text": "Rave Night #12"}]},
                "Date": {"date": {"start": "2026-06-14T20:00:00+00:00"}},
                "DJs": {"relation": [{"id": "old-dj-notion-id"}]},
            },
        }]
    )

    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", AsyncMock())
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=999)
    )

    async def mock_get_page_id(db, user_id):
        return "old-dj-notion-id" if user_id == 999 else "new-dj-notion-id"

    monkeypatch.setattr("bot.services.admin_booking.get_notion_dj_page_id", mock_get_page_id)
    monkeypatch.setattr(
        "bot.services.admin_booking.holds_slot_in_event", AsyncMock(return_value=False)
    )

    success, msg = await svc.book_slot_override(
        admin_name="Admin",
        dj_id=42,
        dj_name="DJ New",
        event_name="Rave Night #12",
        slot_num=1,
    )

    assert success is True
    assert svc.notion_client.write_booking.call_args.args[2] == "new-dj-notion-id"
    svc.notion_client.delete_booking.assert_awaited_once()
    assert svc.notion_client.delete_booking.call_args.args[:2] == ("evt-001", "old-dj-notion-id")
    told = [c.args[0] for c in svc.bot.get_user.call_args_list]
    assert 999 in told and 42 in told  # displaced DJ and new DJ both get a DM


@pytest.mark.asyncio
async def test_book_slot_override_displaced_dj_with_another_slot_stays_linked(monkeypatch):
    """A displaced DJ who still holds another slot of the event keeps the Notion link."""
    svc = _make_service()
    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", AsyncMock())
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=999)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id", AsyncMock(return_value="x")
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.holds_slot_in_event", AsyncMock(return_value=True)
    )

    success, _ = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ New", event_name="Rave Night #12", slot_num=1
    )

    assert success is True
    svc.notion_client.delete_booking.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_slot_override_event_not_found(monkeypatch):
    """Returns (False, error) when event name doesn't match."""
    svc = _make_service()
    svc.notion_client.poll_events = AsyncMock(return_value=[])

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
async def test_book_slot_override_says_so_when_the_displaced_dj_could_not_be_unlinked(monkeypatch):
    """Success, but the admin is told to check Notion instead of being left in the dark."""
    svc = _make_service()
    monkeypatch.setattr("bot.services.admin_booking.admin_force_book_slot", AsyncMock())
    monkeypatch.setattr(
        "bot.services.admin_booking.get_slot_lock_holder", AsyncMock(return_value=999)
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.get_notion_dj_page_id", AsyncMock(return_value="x")
    )
    monkeypatch.setattr(
        "bot.services.admin_booking.holds_slot_in_event",
        AsyncMock(side_effect=ConnectionError("db hiccup")),
    )

    success, msg = await svc.book_slot_override(
        admin_name="Admin", dj_id=42, dj_name="DJ New", event_name="Rave Night #12", slot_num=1
    )

    assert success is True
    assert "check it there" in msg
    svc.notion_client.delete_booking.assert_not_awaited()
