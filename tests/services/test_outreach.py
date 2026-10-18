"""Unit tests for bot/services/outreach.py — OutreachService.run_gap_detection().

All DB and Notion interactions are mocked. Tests verify:
  - Notion failure → returns []
  - Events outside outreach window → skipped
  - New event in window with open slots → sequence upserted, event returned
  - Fully-booked event → mark_sequence_complete called, not returned
  - Active sequence within cooldown → skipped
  - Active sequence past cooldown → returned
  - New sequence (last_contact_at NULL) → returned immediately
  - Complete/exhausted sequence → skipped

[Source: story 5-1-gap-detection-outreach-trigger.md#Task 8]
"""
import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.services.outreach import OutreachService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-001",
    slots: int = 6,
    djs_booked: int = 0,
    days_ahead: int = 7,
) -> dict:
    """Build a minimal Notion event dict for testing."""
    event_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_ahead)
    return {
        "id": event_id,
        "last_edited_time": event_dt.isoformat(),
        "properties": {
            "Name": {"title": [{"plain_text": "Test Event"}]},
            "Date": {"date": {"start": event_dt.date().isoformat()}},
            "Slots": {"number": slots},
            "DJs": {"relation": [{"id": f"dj-{i}"} for i in range(djs_booked)]},
        },
    }


def _make_service(poll_events_result: list | None = None) -> OutreachService:
    bot = MagicMock()
    bot.guilds = [MagicMock(id=1)]
    db = MagicMock()
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(return_value=poll_events_result or [])
    log_service = MagicMock()
    log_service.post_error = AsyncMock()
    log_service.post_dm_transcript = AsyncMock()
    return OutreachService(bot=bot, db=db, notion_client=notion_client, log_service=log_service)


def _active_seq(last_contact_at=None) -> dict:
    return {"event_id": "evt-001", "status": "active", "last_contact_at": last_contact_at}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_gap_detection_fails_the_cycle_on_notion_failure():
    """A Notion outage must not look like 'no gaps': the outreach cycle fails."""
    from bot.loops import CycleFailed

    svc = _make_service()
    svc.notion_client.poll_events = AsyncMock(side_effect=Exception("Notion down"))
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         pytest.raises(CycleFailed, match="Notion down"):
        await svc.run_gap_detection(guild_id=1)


@pytest.mark.asyncio
async def test_run_gap_detection_skips_events_outside_window():
    # Event 20 days ahead, default window is 14 days
    event = _make_event(days_ahead=20)
    svc = _make_service(poll_events_result=[event])
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value="14")), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert result == []


@pytest.mark.asyncio
async def test_run_gap_detection_creates_sequence_for_new_event_with_open_slot():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service(poll_events_result=[event])
    seq = _active_seq(last_contact_at=None)
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()) as mock_upsert, \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    mock_upsert.assert_awaited_once_with(svc.db, "evt-001")
    assert len(result) == 1
    assert result[0]["id"] == "evt-001"


@pytest.mark.asyncio
async def test_run_gap_detection_marks_complete_when_all_slots_filled():
    event = _make_event(days_ahead=5, slots=2, djs_booked=2)  # fully booked
    svc = _make_service(poll_events_result=[event])
    seq = _active_seq()
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()) as mock_complete, \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    mock_complete.assert_awaited_once_with(svc.db, "evt-001")
    assert result == []


@pytest.mark.asyncio
async def test_run_gap_detection_skips_event_within_cooldown():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service(poll_events_result=[event])
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    seq = _active_seq(last_contact_at=recent)
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value="12")), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert result == []  # 3h < 12h cooldown


@pytest.mark.asyncio
async def test_run_gap_detection_returns_event_past_cooldown():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service(poll_events_result=[event])
    old_contact = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=15)
    seq = _active_seq(last_contact_at=old_contact)
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value="12")), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert len(result) == 1
    assert result[0]["id"] == "evt-001"


@pytest.mark.asyncio
async def test_run_gap_detection_returns_new_sequence_null_contact():
    """Newly inserted sequence (last_contact_at=NULL) is ready immediately."""
    event = _make_event(days_ahead=5, slots=6, djs_booked=0)
    svc = _make_service(poll_events_result=[event])
    seq = _active_seq(last_contact_at=None)
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_run_gap_detection_skips_complete_sequence():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service(poll_events_result=[event])
    complete_seq = {"event_id": "evt-001", "status": "complete", "last_contact_at": None}
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=complete_seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert result == []


@pytest.mark.asyncio
async def test_run_gap_detection_skips_exhausted_sequence():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service(poll_events_result=[event])
    exhausted_seq = {"event_id": "evt-001", "status": "exhausted", "last_contact_at": None}
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=exhausted_seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert result == []


# ---------------------------------------------------------------------------
# Story 5.2: contact_next_dj tests
# ---------------------------------------------------------------------------


def _make_dj(discord_user_id=101, dj_name="DJ Test"):
    return {"discord_user_id": discord_user_id, "dj_name": dj_name, "last_contacted_at": None}


@pytest.mark.asyncio
async def test_contact_next_dj_returns_false_when_no_eligible_djs():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service()
    ctx = MagicMock()
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])):
        result = await svc.contact_next_dj(event, guild_id=1, ctx=ctx)
    assert result is False


@pytest.mark.asyncio
async def test_contact_next_dj_sends_dm_and_returns_true():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service()
    ctx = MagicMock()
    ctx.get_user_lock = MagicMock()
    dj = _make_dj(discord_user_id=101)
    mock_user = MagicMock()
    mock_user.send = AsyncMock()
    svc.bot.get_user = MagicMock(return_value=mock_user)
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[dj])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()), \
         patch("bot.services.outreach.update_last_contact_at", new=AsyncMock()), \
         patch("bot.services.outreach.update_dj_last_contacted_at", new=AsyncMock()):
        result = await svc.contact_next_dj(event, guild_id=1, ctx=ctx)
    assert result is True
    mock_user.send.assert_awaited_once()
    # Story 6.1 AC1: outgoing DM must be transcribed
    svc.log_service.post_dm_transcript.assert_awaited_once()
    call_kwargs = svc.log_service.post_dm_transcript.call_args.kwargs
    assert call_kwargs["direction"] == "BOT"
    assert "DJ Test" in call_kwargs["dj_name"]


@pytest.mark.asyncio
async def test_contact_next_dj_handles_forbidden_dm():
    import disnake
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service()
    ctx = MagicMock()
    dj = _make_dj(discord_user_id=101)
    mock_user = MagicMock()
    mock_user.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(), "DMs blocked"))
    svc.bot.get_user = MagicMock(return_value=mock_user)
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[dj])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()), \
         patch("bot.services.outreach.update_outreach_contact_status", new=AsyncMock()) as mock_status:
        result = await svc.contact_next_dj(event, guild_id=1, ctx=ctx)
    assert result is True
    mock_status.assert_awaited()
    # AC4: ERR_DM_FAILED must be logged to admin channel
    svc.log_service.post_error.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_exhaustion — Story 5.3 AC1, AC2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_exhaustion_marks_exhausted_and_posts_alert():
    """Active sequence with open slots → marked exhausted + ERR_OUTREACH_EXHAUSTED posted."""
    event = _make_event(event_id="evt-001", slots=6, djs_booked=2, days_ahead=5)
    svc = _make_service()
    active_seq = {"event_id": "evt-001", "status": "active", "last_contact_at": None}
    with patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=active_seq)), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[101, 102])), \
         patch("bot.services.outreach.mark_sequence_exhausted", new=AsyncMock()) as mock_exhaust:
        await svc.handle_exhaustion(event, guild_id=1)
    mock_exhaust.assert_awaited_once_with(svc.db, "evt-001")
    svc.log_service.post_error.assert_awaited_once()
    call_kwargs = svc.log_service.post_error.call_args
    assert call_kwargs.kwargs["error_code"].value == "ERR_OUTREACH_EXHAUSTED"
    assert "2" in call_kwargs.kwargs["trigger"]   # djs_contacted_count
    assert "4" in call_kwargs.kwargs["trigger"]   # open_slots (6 - 2)


@pytest.mark.asyncio
async def test_handle_exhaustion_skips_if_already_exhausted():
    """Sequence already exhausted → no DB write, no alert (AC2 duplicate-prevention)."""
    event = _make_event(event_id="evt-001", slots=6, djs_booked=2, days_ahead=5)
    svc = _make_service()
    exhausted_seq = {"event_id": "evt-001", "status": "exhausted", "last_contact_at": None}
    with patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=exhausted_seq)), \
         patch("bot.services.outreach.mark_sequence_exhausted", new=AsyncMock()) as mock_exhaust:
        await svc.handle_exhaustion(event, guild_id=1)
    mock_exhaust.assert_not_awaited()
    svc.log_service.post_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_exhaustion_skips_if_no_open_slots():
    """All slots booked → no exhaustion alert (race condition guard)."""
    event = _make_event(event_id="evt-001", slots=6, djs_booked=6, days_ahead=5)
    svc = _make_service()
    active_seq = {"event_id": "evt-001", "status": "active", "last_contact_at": None}
    with patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=active_seq)), \
         patch("bot.services.outreach.mark_sequence_exhausted", new=AsyncMock()) as mock_exhaust:
        await svc.handle_exhaustion(event, guild_id=1)
    mock_exhaust.assert_not_awaited()
    svc.log_service.post_error.assert_not_awaited()


# ---------------------------------------------------------------------------
# run_gap_detection AC3 — reactivate exhausted sequence with new DJ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_gap_detection_reactivates_exhausted_seq_with_new_dj():
    """Exhausted sequence + new active DJ not in contacts → reactivated, event returned (AC3)."""
    event = _make_event(event_id="evt-001", slots=6, djs_booked=2, days_ahead=5)
    svc = _make_service(poll_events_result=[event])
    exhausted_seq = {"event_id": "evt-001", "status": "exhausted", "last_contact_at": None}
    active_seq = {"event_id": "evt-001", "status": "active", "last_contact_at": None}
    new_dj = {"discord_user_id": 999, "dj_name": "New DJ", "last_contacted_at": None}

    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(
             side_effect=[exhausted_seq, active_seq]
         )), \
         patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[new_dj])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.reactivate_sequence", new=AsyncMock()) as mock_reactivate:
        result = await svc.run_gap_detection(guild_id=1)

    mock_reactivate.assert_awaited_once_with(svc.db, "evt-001")
    assert len(result) == 1
    assert result[0]["id"] == "evt-001"


@pytest.mark.asyncio
async def test_contact_next_dj_marks_unreachable_if_user_not_found():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service()
    ctx = MagicMock()
    dj = _make_dj(discord_user_id=101)
    svc.bot.get_user = MagicMock(return_value=None)
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[dj])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()), \
         patch("bot.services.outreach.update_outreach_contact_status", new=AsyncMock()) as mock_status:
        result = await svc.contact_next_dj(event, guild_id=1, ctx=ctx)
    assert result is True
    mock_status.assert_awaited()
    # AC4: ERR_DM_FAILED must be logged to admin channel
    svc.log_service.post_error.assert_awaited_once()
