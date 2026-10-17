"""Background loops keep running after errors; lineup and outreach do not stall."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot import loops


@pytest.fixture(autouse=True)
def _fresh_loop_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loops, "_last_ok", {})
    monkeypatch.setattr(loops, "_last_alert", {})


@pytest.mark.asyncio
async def test_guarded_swallows_errors_and_alerts_once_per_hour() -> None:
    log = AsyncMock()
    failing = AsyncMock(side_effect=KeyError("Date"))
    with patch("bot.loops.time.monotonic", return_value=10_000.0):
        await loops.guarded("notion_poll", failing, log)
        await loops.guarded("notion_poll", failing, log)
    with patch("bot.loops.time.monotonic", return_value=10_000.0 + loops.ALERT_INTERVAL_SECONDS):
        await loops.guarded("notion_poll", failing, log)
    assert failing.await_count == 3
    assert log.post_error.await_count == 2
    assert "notion_poll" not in loops.last_ok()


@pytest.mark.asyncio
async def test_guarded_records_last_success() -> None:
    await loops.guarded("reminders", AsyncMock(), None)
    assert "reminders" in loops.last_ok()


def _events_cog(lineup_result):
    from bot.cogs.events import EventsCog

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.log_service = AsyncMock()
    ctx.notion_client = AsyncMock()
    ctx.notion_client.poll_events = AsyncMock(
        return_value=[{"id": "evt", "last_edited_time": "2026-10-17T10:00:00.000Z"}]
    )
    ctx.lineup_service = AsyncMock()
    if isinstance(lineup_result, Exception):
        ctx.lineup_service.post_update = AsyncMock(side_effect=lineup_result)
    else:
        ctx.lineup_service.post_update = AsyncMock(return_value=lineup_result)
    guild = MagicMock()
    guild.id = 42
    bot = MagicMock()
    bot.guilds = [guild]
    return EventsCog(ctx, bot), ctx


async def _run_poll(cog) -> AsyncMock:
    from bot.cogs.events import EventsCog

    set_value = AsyncMock()
    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=None)), patch(
        "bot.cogs.events.set_value", new=set_value
    ), patch("bot.schedule.refresh", new=AsyncMock()), patch.object(
        EventsCog, "_guard_db", new=AsyncMock(return_value=True)
    ):
        await EventsCog._poll_notion.coro(cog)
    return set_value


@pytest.mark.asyncio
async def test_forbidden_lineup_post_does_not_end_the_poll_loop() -> None:
    """Repro F: one 403 used to stop _poll_notion for good."""
    forbidden = disnake.Forbidden(MagicMock(status=403), "Missing Permissions")
    cog, ctx = _events_cog(forbidden)
    set_value = await _run_poll(cog)  # must not raise
    snapshot_writes = [c for c in set_value.await_args_list if c.args[2] == "lineup_snapshot"]
    assert snapshot_writes == []
    ctx.log_service.post_error.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("posted, saved", [(True, 1), (False, 0)])
async def test_snapshot_saved_only_when_lineup_is_current(posted: bool, saved: int) -> None:
    """Repro E: a capped or failed post must be retried, not marked as handled."""
    cog, _ = _events_cog(posted)
    set_value = await _run_poll(cog)
    snapshot_writes = [c for c in set_value.await_args_list if c.args[2] == "lineup_snapshot"]
    assert len(snapshot_writes) == saved


@pytest.mark.asyncio
async def test_outreach_skips_intervened_and_already_booked_djs() -> None:
    """Repro G: the queue stalled behind a DJ under /intervene."""
    from bot import slots
    from bot.services.outreach import OutreachService

    svc = OutreachService(bot=MagicMock(), db=AsyncMock(), notion_client=AsyncMock(),
                          log_service=AsyncMock())
    event = {"id": "evt", "properties": {}}
    event["_ravebot_slots"] = slots.SlotState(total=6, booked={0: 2})  # DJ 2 already plays
    djs = [{"discord_user_id": i, "dj_name": f"dj{i}"} for i in (1, 2, 3)]
    record = AsyncMock()
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=djs)), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.is_under_intervention",
               new=AsyncMock(side_effect=lambda db, uid: uid == 1)), \
         patch("bot.services.outreach.record_outreach_contact", new=record), \
         patch("bot.slots.annotate", new=AsyncMock(side_effect=lambda p, g, ev: ev)):
        svc.bot.get_user = MagicMock(return_value=None)  # stop after the contact is recorded
        await svc.contact_next_dj(event, 42, MagicMock())
    assert record.await_args.args[1:] == ("evt", 3)


@pytest.mark.asyncio
async def test_notion_outage_is_not_ok_and_alerts_once_an_hour() -> None:
    """A failing poll keeps its specific wording, is not 'last OK', and does not flood."""
    from bot import guild as served
    from bot.cogs.events import EventsCog
    from bot.ui.embeds import ErrorCode

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.log_service.post_error = AsyncMock()
    ctx.notion_client.poll_events = AsyncMock(side_effect=RuntimeError("notion 502"))
    cog = EventsCog(ctx, MagicMock())
    with patch("bot.schedule.refresh", AsyncMock()), \
         patch.object(served, "resolve", lambda bot: 42), \
         patch.object(EventsCog, "_guard_db", AsyncMock(return_value=True)):
        for _ in range(3):
            await EventsCog._poll_notion.coro(cog)
    assert "notion_poll" not in loops.last_ok()
    ctx.log_service.post_error.assert_awaited_once()
    kwargs = ctx.log_service.post_error.call_args.kwargs
    assert kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE
    assert "notion 502" in kwargs["trigger"]


@pytest.mark.asyncio
async def test_reminder_failure_is_reported_and_not_ok() -> None:
    from bot import guild as served
    from bot.cogs.events import EventsCog

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.log_service.post_error = AsyncMock()
    ctx.reminders_service.send_reminders = AsyncMock(side_effect=RuntimeError("boom"))
    cog = EventsCog(ctx, MagicMock())
    with patch.object(served, "resolve", lambda bot: 42), \
         patch.object(EventsCog, "_guard_db", AsyncMock(return_value=True)):
        await EventsCog._check_reminders.coro(cog)
    assert "reminders" not in loops.last_ok()
    ctx.log_service.post_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_db_outage_is_skipped_without_a_second_alert_and_not_ok() -> None:
    from bot.cogs.events import EventsCog

    ctx = MagicMock()
    ctx.db = None
    ctx.log_service.post_error = AsyncMock()
    cog = EventsCog(ctx, MagicMock())
    for _ in range(3):
        await EventsCog._poll_notion.coro(cog)
    assert "notion_poll" not in loops.last_ok()
    ctx.log_service.post_error.assert_awaited_once()  # from the DB guard, once an hour
