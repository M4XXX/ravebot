"""Unit tests for bot/services/reminders.py — RemindersService.send_reminders().

Tests use a fixed 'now' of 2026-05-21 02:00 UTC so that:
  - event date "2026-05-22" midnight = 22h from FIXED_NOW  → IN window (20-28h)
  - event date "2026-05-23" midnight = 46h from FIXED_NOW  → outside window (> 28h)
  - event date "2026-05-21" midnight = -2h from FIXED_NOW  → outside window (past)

The injectable _now parameter in send_reminders() avoids datetime patching entirely.

[Source: story 5-4-day-before-event-reminder.md#Tasks 8]
"""
import datetime
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.services.reminders import RemindersService
from bot.ui.embeds import ErrorCode

# Fixed reference: 2026-05-21 02:00 UTC
# "2026-05-22" midnight = 22h away → squarely in the 20-28h window
FIXED_NOW = datetime.datetime(2026, 5, 21, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_id: str = "evt-001", event_date: str = "2026-05-22") -> dict:
    """Build a minimal Notion event dict."""
    return {
        "id": event_id,
        "properties": {
            "Name": {"title": [{"plain_text": "Test Event"}]},
            "Date": {"date": {"start": event_date}},
            "Slots": {"number": 6},
            "DJs": {"relation": []},
        },
    }


def _make_service(poll_events_result: list | None = None) -> RemindersService:
    bot = MagicMock()
    db = MagicMock()
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(return_value=poll_events_result or [])
    log_service = MagicMock()
    log_service.post_error = AsyncMock()
    return RemindersService(
        bot=bot, db=db, notion_client=notion_client, log_service=log_service
    )


# ---------------------------------------------------------------------------
# _is_in_reminder_window unit tests (no async needed)
# ---------------------------------------------------------------------------


def test_is_in_reminder_window_true_for_22h_ahead():
    """event_date '2026-05-22' midnight = 22h from FIXED_NOW → True."""
    event = _make_event(event_date="2026-05-22")
    assert RemindersService._is_in_reminder_window(event, FIXED_NOW) is True


def test_is_in_reminder_window_false_for_46h_ahead():
    """event_date '2026-05-23' midnight = 46h from FIXED_NOW → False (> 28h)."""
    event = _make_event(event_date="2026-05-23")
    assert RemindersService._is_in_reminder_window(event, FIXED_NOW) is False


def test_is_in_reminder_window_false_for_past_event():
    """event_date '2026-05-21' midnight is 2h in the past → False."""
    event = _make_event(event_date="2026-05-21")
    assert RemindersService._is_in_reminder_window(event, FIXED_NOW) is False


def test_is_in_reminder_window_false_for_malformed_date():
    """Missing Date property → False (no crash)."""
    event = {"id": "evt-bad", "properties": {}}
    assert RemindersService._is_in_reminder_window(event, FIXED_NOW) is False


# ---------------------------------------------------------------------------
# send_reminders integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_reminders_skips_on_notion_failure():
    """Notion poll raises → no crash, no reminders sent."""
    svc = _make_service()
    svc.notion_client.poll_events = AsyncMock(side_effect=Exception("API down"))

    with patch("bot.services.reminders.get_booked_djs_for_event", new=AsyncMock()) as mock_get:
        await svc.send_reminders(guild_id=1, ctx=MagicMock(), _now=FIXED_NOW)

    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_reminders_skips_event_outside_window():
    """Event date 46h away → booked DJs never queried."""
    event = _make_event(event_date="2026-05-23")  # 46h away
    svc = _make_service(poll_events_result=[event])

    with patch("bot.services.reminders.get_booked_djs_for_event", new=AsyncMock()) as mock_get:
        await svc.send_reminders(guild_id=1, ctx=MagicMock(), _now=FIXED_NOW)

    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_reminders_sends_dm_and_records_sent():
    """Event in window, DJ booked, no prior reminder → DM sent + reminder recorded (AC2)."""
    event = _make_event(event_id="evt-001", event_date="2026-05-22")
    svc = _make_service(poll_events_result=[event])

    user_mock = MagicMock()
    user_mock.send = AsyncMock()
    svc.bot.get_user = MagicMock(return_value=user_mock)

    booked_dj = {
        "discord_user_id": 123,
        "dj_name": "DJ Test",
        "slot_index": 0,
        "event_id": "evt-001",
    }

    with patch(
        "bot.services.reminders.get_booked_djs_for_event",
        new=AsyncMock(return_value=[booked_dj]),
    ), patch(
        "bot.services.reminders.has_reminder_been_sent",
        new=AsyncMock(return_value=False),
    ), patch(
        "bot.services.reminders.record_reminder_sent",
        new=AsyncMock(),
    ) as mock_record, patch(
        "bot.services.reminders.get_value",
        new=AsyncMock(return_value=None),
    ):
        await svc.send_reminders(guild_id=1, ctx=MagicMock(), _now=FIXED_NOW)

    user_mock.send.assert_awaited_once()
    dm_text = user_mock.send.call_args[0][0]
    assert "tomorrow at" in dm_text
    assert "\U0001f3a7" in dm_text  # 🎧
    mock_record.assert_awaited_once_with(svc.db, "evt-001", 123, 0)


@pytest.mark.asyncio
async def test_send_reminders_skips_if_already_sent():
    """Reminder already recorded → DJ skipped, no DM (AC2, AC4)."""
    event = _make_event(event_date="2026-05-22")
    svc = _make_service(poll_events_result=[event])

    user_mock = MagicMock()
    user_mock.send = AsyncMock()
    svc.bot.get_user = MagicMock(return_value=user_mock)

    booked_dj = {
        "discord_user_id": 123,
        "dj_name": "DJ Test",
        "slot_index": 0,
        "event_id": "evt-001",
    }

    with patch(
        "bot.services.reminders.get_booked_djs_for_event",
        new=AsyncMock(return_value=[booked_dj]),
    ), patch(
        "bot.services.reminders.has_reminder_been_sent",
        new=AsyncMock(return_value=True),
    ):
        await svc.send_reminders(guild_id=1, ctx=MagicMock(), _now=FIXED_NOW)

    user_mock.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_reminders_handles_dm_forbidden():
    """DM blocked (Forbidden) → ERR_DM_FAILED posted, reminder NOT recorded (AC3)."""
    import disnake

    event = _make_event(event_id="evt-001", event_date="2026-05-22")
    svc = _make_service(poll_events_result=[event])

    resp_mock = MagicMock()
    resp_mock.status = 403
    resp_mock.reason = "Forbidden"
    user_mock = MagicMock()
    user_mock.send = AsyncMock(
        side_effect=disnake.Forbidden(resp_mock, "Cannot send messages to this user")
    )
    svc.bot.get_user = MagicMock(return_value=user_mock)

    booked_dj = {
        "discord_user_id": 123,
        "dj_name": "DJ Test",
        "slot_index": 0,
        "event_id": "evt-001",
    }

    with patch(
        "bot.services.reminders.get_booked_djs_for_event",
        new=AsyncMock(return_value=[booked_dj]),
    ), patch(
        "bot.services.reminders.has_reminder_been_sent",
        new=AsyncMock(return_value=False),
    ), patch(
        "bot.services.reminders.record_reminder_sent",
        new=AsyncMock(),
    ) as mock_record, patch(
        "bot.services.reminders.get_value",
        new=AsyncMock(return_value=None),
    ):
        await svc.send_reminders(guild_id=1, ctx=MagicMock(), _now=FIXED_NOW)

    # Reminder must NOT be recorded on DM failure (AC3)
    mock_record.assert_not_awaited()

    # ERR_DM_FAILED must be posted with DJ name, user ID, and slot time
    svc.log_service.post_error.assert_awaited_once()
    call_kwargs = svc.log_service.post_error.call_args.kwargs
    assert call_kwargs["error_code"] == ErrorCode.ERR_DM_FAILED
    assert "123" in call_kwargs["trigger"]
    assert "DJ Test" in call_kwargs["trigger"]
    assert "<t:" in call_kwargs["trigger"]  # AC3: slot time required in trigger


@pytest.mark.asyncio
async def test_send_reminders_skips_user_not_in_cache():
    """bot.get_user returns None → DJ skipped, no crash, no reminder recorded."""
    event = _make_event(event_date="2026-05-22")
    svc = _make_service(poll_events_result=[event])
    svc.bot.get_user = MagicMock(return_value=None)

    booked_dj = {
        "discord_user_id": 456,
        "dj_name": "Ghost DJ",
        "slot_index": 1,
        "event_id": "evt-001",
    }

    with patch(
        "bot.services.reminders.get_booked_djs_for_event",
        new=AsyncMock(return_value=[booked_dj]),
    ), patch(
        "bot.services.reminders.has_reminder_been_sent",
        new=AsyncMock(return_value=False),
    ), patch(
        "bot.services.reminders.record_reminder_sent",
        new=AsyncMock(),
    ) as mock_record, patch(
        "bot.services.reminders.get_value",
        new=AsyncMock(return_value=None),
    ):
        await svc.send_reminders(guild_id=1, ctx=MagicMock(), _now=FIXED_NOW)

    mock_record.assert_not_awaited()
