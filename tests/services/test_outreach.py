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

from bot.notion.models import EventInfo
from bot.services.outreach import OutreachService
from tests.notion.factories import make_event, make_slot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "Test Event",
    slots: int = 6,
    djs_booked: int = 0,
    days_ahead: int = 7,
    page_id: str = "evt-001",
) -> EventInfo:
    """Build a typed event with djs_booked filled slots (slots 1..djs_booked)."""
    event_date = (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days_ahead)
    ).date()
    return make_event(
        event_id=event_id,
        page_id=page_id,
        date=event_date,
        slot_count=slots,
        slots=[
            make_slot(number=i + 1, dj_page_id=f"dj-{i}", dj_name=f"DJ {i}")
            for i in range(djs_booked)
        ],
    )


def _make_service(list_events_result: list | None = None) -> OutreachService:
    bot = MagicMock()
    bot.guilds = [MagicMock(id=1)]
    db = MagicMock()
    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(return_value=list_events_result or [])
    log_service = MagicMock()
    log_service.post_error = AsyncMock()
    return OutreachService(bot=bot, db=db, notion_client=notion_client, log_service=log_service)


def _active_seq(last_contact_at=None) -> dict:
    return {"event_id": "evt-001", "status": "active", "last_contact_at": last_contact_at}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_gap_detection_returns_empty_on_notion_failure():
    svc = _make_service()
    svc.notion_client.list_events = AsyncMock(side_effect=Exception("Notion down"))
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)):
        result = await svc.run_gap_detection(guild_id=1)
    assert result == []


@pytest.mark.asyncio
async def test_run_gap_detection_skips_events_outside_window():
    # Event 20 days ahead, default window is 14 days
    event = _make_event(days_ahead=20)
    svc = _make_service(list_events_result=[event])
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value="14")), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert result == []


@pytest.mark.asyncio
async def test_run_gap_detection_creates_sequence_for_new_event_with_open_slot():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service(list_events_result=[event])
    seq = _active_seq(last_contact_at=None)
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()) as mock_upsert, \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    mock_upsert.assert_awaited_once_with(svc.db, "evt-001")
    assert len(result) == 1
    assert result[0].page_id == "evt-001"


@pytest.mark.asyncio
async def test_run_gap_detection_marks_complete_when_all_slots_filled():
    event = _make_event(days_ahead=5, slots=2, djs_booked=2)  # fully booked
    svc = _make_service(list_events_result=[event])
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
    svc = _make_service(list_events_result=[event])
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
    svc = _make_service(list_events_result=[event])
    old_contact = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=15)
    seq = _active_seq(last_contact_at=old_contact)
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value="12")), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()):
        result = await svc.run_gap_detection(guild_id=1)
    assert len(result) == 1
    assert result[0].page_id == "evt-001"


@pytest.mark.asyncio
async def test_run_gap_detection_returns_new_sequence_null_contact():
    """Newly inserted sequence (last_contact_at=NULL) is ready immediately."""
    event = _make_event(days_ahead=5, slots=6, djs_booked=0)
    svc = _make_service(list_events_result=[event])
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
    svc = _make_service(list_events_result=[event])
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
    svc = _make_service(list_events_result=[event])
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


@pytest.fixture(autouse=True)
def _nobody_waiting():
    with patch("bot.services.outreach.get_waiting_discord_ids", new=AsyncMock(return_value=[])):
        yield


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


@pytest.mark.asyncio
async def test_contact_next_dj_dm_has_opener_lineup_and_view():
    from bot.ui import texts
    from bot.ui.embeds import LineupUpdateEmbed
    from bot.ui.views import OutreachView

    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service()
    mock_user = MagicMock()
    mock_user.send = AsyncMock()
    svc.bot.get_user = MagicMock(return_value=mock_user)
    djs = AsyncMock(return_value=[_make_dj()])
    with patch("bot.services.outreach.get_active_booking_djs", new=djs), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()), \
         patch("bot.services.outreach.update_last_contact_at", new=AsyncMock()), \
         patch("bot.services.outreach.update_dj_last_contacted_at", new=AsyncMock()):
        await svc.contact_next_dj(event, guild_id=1, ctx=MagicMock())
    kwargs = mock_user.send.call_args.kwargs
    assert kwargs["content"] == texts.invite_opener(event.event_id)
    assert isinstance(kwargs["embed"], LineupUpdateEmbed)
    assert "open slot" in kwargs["embed"].description
    assert "DJ 0" in kwargs["embed"].description
    assert isinstance(kwargs["view"], OutreachView)


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
    svc = _make_service(list_events_result=[event])
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
    assert result[0].page_id == "evt-001"


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


# ---------------------------------------------------------------------------
# Typed-event slot counting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_slots_counted_from_slot_rows_dj_with_two_slots_counts_two():
    """Slots 1 and 2 held by the same DJ, 3 total -> one open slot, event is ready."""
    event = make_event(
        page_id="evt-001",
        date=(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=5)).date(),
        slot_count=3,
        slots=[
            make_slot(number=1, dj_page_id="dj-a"),
            make_slot(number=2, dj_page_id="dj-a"),
            make_slot(number=3),
        ],
    )
    svc = _make_service(list_events_result=[event])
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=_active_seq())), \
         patch("bot.services.outreach.mark_sequence_complete", new=AsyncMock()) as mock_complete:
        result = await svc.run_gap_detection(guild_id=1)
    assert [e.page_id for e in result] == ["evt-001"]
    mock_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_slots_gap_in_middle_counts_as_open():
    """Slots 1 and 3 filled, 2 empty, 3 total -> a gap is open, not complete."""
    event = make_event(
        page_id="evt-001",
        date=(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=5)).date(),
        slot_count=3,
        slots=[
            make_slot(number=1, dj_page_id="dj-a"),
            make_slot(number=2),
            make_slot(number=3, dj_page_id="dj-b"),
        ],
    )
    svc = _make_service(list_events_result=[event])
    with patch("bot.services.outreach.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.services.outreach.upsert_sequence", new=AsyncMock()), \
         patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=_active_seq())):
        result = await svc.run_gap_detection(guild_id=1)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_handle_exhaustion_open_slots_from_slot_rows():
    """Two slots held by one DJ out of 4 -> 2 open slots reported."""
    event = make_event(
        event_id="Rave", page_id="evt-001", slot_count=4,
        slots=[make_slot(number=1, dj_page_id="dj-a"), make_slot(number=2, dj_page_id="dj-a")],
    )
    svc = _make_service()
    with patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=_active_seq())), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.mark_sequence_exhausted", new=AsyncMock()):
        await svc.handle_exhaustion(event, guild_id=1)
    trigger = svc.log_service.post_error.call_args.kwargs["trigger"]
    assert "2 slots are still open" in trigger
    assert "Rave" in trigger


@pytest.mark.asyncio
async def test_contact_next_dj_skips_dj_with_unanswered_invite():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service()
    user = MagicMock(send=AsyncMock())
    svc.bot.get_user = MagicMock(return_value=user)
    djs = [_make_dj(101, "Busy"), _make_dj(102, "Free")]
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=djs)), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.get_waiting_discord_ids", new=AsyncMock(return_value=[101])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()) as record, \
         patch("bot.services.outreach.update_last_contact_at", new=AsyncMock()), \
         patch("bot.services.outreach.update_dj_last_contacted_at", new=AsyncMock()):
        assert await svc.contact_next_dj(event, guild_id=1, ctx=MagicMock()) is True
    record.assert_awaited_once_with(svc.db, event.page_id, 102)


@pytest.mark.asyncio
async def test_contact_next_dj_waits_when_only_waiting_djs_remain():
    event = _make_event(days_ahead=5, slots=6, djs_booked=2)
    svc = _make_service()
    user = MagicMock(send=AsyncMock())
    svc.bot.get_user = MagicMock(return_value=user)
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[_make_dj(101)])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.get_waiting_discord_ids", new=AsyncMock(return_value=[101])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()) as record:
        # True: not exhausted, the next cycle tries again
        assert await svc.contact_next_dj(event, guild_id=1, ctx=MagicMock()) is True
    record.assert_not_awaited()
    user.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_next_dj_skips_djs_booked_in_the_event():
    event = _make_event(days_ahead=5, slots=6, djs_booked=0)
    event = make_event(
        event_id=event.event_id, page_id=event.page_id, date=event.date, slot_count=6,
        slots=[make_slot(1, dj_page_id="p", dj_name="Booked", discord_id=101)],
    )
    svc = _make_service()
    svc.bot.get_user = MagicMock(return_value=MagicMock(send=AsyncMock()))
    djs = [_make_dj(101, "Booked"), _make_dj(102, "Free")]
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=djs)), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()) as record, \
         patch("bot.services.outreach.update_last_contact_at", new=AsyncMock()), \
         patch("bot.services.outreach.update_dj_last_contacted_at", new=AsyncMock()):
        await svc.contact_next_dj(event, guild_id=1, ctx=MagicMock())
    assert record.await_args.args[2] == 102


def test_open_slots_ignores_duplicate_rows_and_rows_above_the_count():
    event = make_event(slot_count=2, slots=[
        make_slot(1, page_id="a"), make_slot(1, dj_page_id="d", page_id="b"),
        make_slot(3, dj_page_id="d", page_id="c"),
    ])
    assert OutreachService._open_slots(event) == 2
