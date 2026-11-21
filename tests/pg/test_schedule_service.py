"""Real-SQL checks of bot/services/schedule.py (pending changes, apply, replenish)."""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.context import BotContext
from bot.db import events as db_events
from bot.db.config import set_value
from bot.db.manual_events import mark_manual
from bot.db.schedule_rules import insert_rule, list_rules, set_default
from bot.services.event_replenish import replenish_events
from bot.services.schedule import ScheduleService, schedule_guild_id
from bot.services.schedule_rules import Rule
from bot.timezone import local_today

GUILD = 42
WEEK = datetime.timedelta(days=7)
@pytest.fixture
def ctx(pool):
    ctx = BotContext(config=MagicMock(), db=pool)
    ctx.lineup_service = SimpleNamespace(refresh=AsyncMock())
    return ctx


async def _event(pool, day: datetime.date, **kw):
    draft = db_events.EventDraft(
        event_id=f"{day.isoformat()} Rave Night", date=day,
        start=datetime.time(20, 0), slot_minutes=60, slot_count=3, **kw,
    )
    return await db_events.insert_event(pool, draft)


def _summary(pending):
    return [(p.event.page_id, p.field, p.old, p.new) for p in pending]


async def test_pending_lists_changes_and_skips_hand_values(pool, ctx) -> None:
    today = local_today()
    e1 = await _event(pool, today + WEEK)
    e2 = await _event(pool, today + 2 * WEEK, genre="Mine", genre_by_hand=True)
    await insert_rule(pool, GUILD, Rule(None, "genre", "Techno", 1, e1.date))
    await set_default(pool, GUILD, "location", "Club")
    pending = await ScheduleService(ctx).pending_changes(GUILD)
    assert _summary(pending) == [
        (e1.page_id, "genre", None, "Techno"),
        (e1.page_id, "location", None, "Club"),
        (e2.page_id, "location", None, "Club"),
    ]


async def test_pending_skips_manual_past_and_finished(pool, ctx) -> None:
    today = local_today()
    await _event(pool, today - WEEK)
    manual = await _event(pool, today + WEEK)
    await mark_manual(pool, manual.page_id)
    finished = await _event(pool, today + 2 * WEEK, status="Done")
    normal = await _event(pool, today + 3 * WEEK)
    await set_default(pool, GUILD, "genre", "Techno")
    svc = ScheduleService(ctx)
    assert _summary(await svc.pending_changes(GUILD)) == [
        (normal.page_id, "genre", None, "Techno")
    ]
    dates = await svc.upcoming_dates(GUILD, 3)
    listed = {d.event.page_id for d in dates if d.event is not None}
    assert listed == {normal.page_id}
    assert manual.page_id not in listed and finished.page_id not in listed
    assert [d.date for d in dates] == [normal.date, normal.date + WEEK, normal.date + 2 * WEEK]


async def test_apply_all_and_picked(pool, ctx) -> None:
    today = local_today()
    e1, e2, e3 = [await _event(pool, today + n * WEEK) for n in (1, 2, 3)]
    await set_default(pool, GUILD, "genre", "Techno")
    svc = ScheduleService(ctx)
    assert await svc.apply(GUILD, [(e1.page_id, "genre"), ("unknown", "genre")]) == 1
    assert (await db_events.get_event(pool, e1.page_id)).genre == "Techno"
    assert (await db_events.get_event(pool, e2.page_id)).genre is None
    ctx.lineup_service.refresh.assert_awaited_once()
    assert await svc.apply(GUILD, None) == 2
    for event in (e2, e3):
        stored = await db_events.get_event(pool, event.page_id)
        assert stored.genre == "Techno" and stored.genre_by_hand is False
    assert await svc.pending_changes(GUILD) == []
    assert await svc.apply(GUILD, None) == 0


async def test_apply_recomputes_at_press_time(pool, ctx) -> None:
    today = local_today()
    e1 = await _event(pool, today + WEEK)
    e2 = await _event(pool, today + 2 * WEEK)
    svc = ScheduleService(ctx)
    rule = await svc.add_rule(GUILD, Rule(None, "genre", "Techno", 1, e1.date))
    pending = await svc.pending_changes(GUILD)
    picks = [(p.event.page_id, p.field) for p in pending]
    assert len(picks) == 2
    await svc.set_event_values(GUILD, e1.page_id, genre="Mine")
    await svc.edit_rule(GUILD, Rule(rule.id, "genre", "House", 1, rule.anchor))
    assert await svc.apply(GUILD, picks) == 1
    first = await db_events.get_event(pool, e1.page_id)
    assert (first.genre, first.genre_by_hand) == ("Mine", True)
    assert (await db_events.get_event(pool, e2.page_id)).genre == "House"


async def test_keep_reoffers_on_next_change(pool, ctx) -> None:
    today = local_today()
    e1 = await _event(pool, today + WEEK)
    svc = ScheduleService(ctx)
    rule = await svc.add_rule(GUILD, Rule(None, "genre", "Techno", 1, e1.date))
    assert _summary(await svc.pending_changes(GUILD)) == [(e1.page_id, "genre", None, "Techno")]
    # Keep: nothing applied; the next change offers the event again with the new value.
    await svc.edit_rule(GUILD, Rule(rule.id, "genre", "House", 1, rule.anchor))
    assert _summary(await svc.pending_changes(GUILD)) == [(e1.page_id, "genre", None, "House")]
    assert await svc.apply(GUILD, None) == 1
    await svc.edit_rule(GUILD, Rule(rule.id, "genre", "Trance", 1, rule.anchor))
    assert _summary(await svc.pending_changes(GUILD)) == [
        (e1.page_id, "genre", "House", "Trance")
    ]
    assert await svc.remove_rule(GUILD, rule.id) is True
    assert _summary(await svc.pending_changes(GUILD)) == [(e1.page_id, "genre", "House", None)]


async def test_replenish_resolves_new_events(pool, ctx) -> None:
    await set_value(pool, GUILD, "event_lookahead_count", "4")
    svc = ScheduleService(ctx)
    first = (await svc.upcoming_dates(GUILD, 1))[0]
    assert first.event is None
    await svc.add_rule(GUILD, Rule(None, "genre", "Techno", 2, first.date))
    await svc.set_default(GUILD, "genre", "House")
    await svc.set_default(GUILD, "location", "Club")
    assert await replenish_events(ctx, GUILD) == 4
    events = await db_events.list_events(pool)
    assert [e.date for e in events][0] == first.date
    assert [e.genre for e in events] == ["Techno", "House", "Techno", "House"]
    assert {e.location for e in events} == {"Club"}
    assert not any(e.genre_by_hand or e.location_by_hand for e in events)
    assert await svc.pending_changes(GUILD) == []


async def test_schedule_guild_id(pool) -> None:
    assert await schedule_guild_id(pool) is None
    await set_value(pool, 9, "default_genre", "")
    assert await schedule_guild_id(pool) is None
    await set_default(pool, 9, "location", "Club")
    assert await schedule_guild_id(pool) == 9
    await insert_rule(pool, 5, Rule(None, "genre", "Techno", 1, local_today()))
    assert await schedule_guild_id(pool) == 5


async def test_reanchor_rules_moves_anchors(pool, ctx) -> None:
    today = local_today()
    a = await insert_rule(pool, GUILD, Rule(None, "genre", "Techno", 1, today + WEEK))
    b = await insert_rule(pool, GUILD, Rule(None, "location", "Club", 2, today + 2 * WEEK))
    svc = ScheduleService(ctx)
    origin = today + datetime.timedelta(days=3)
    assert await svc.check_reanchor(GUILD, 14, origin) == []
    await svc.reanchor_rules(GUILD, 14, origin)
    rules = {r.id: r for r in await list_rules(pool, GUILD)}
    assert rules[a.id].anchor == origin + datetime.timedelta(days=14)
    assert rules[b.id].anchor == origin + datetime.timedelta(days=14)
