"""Unit tests for bot/services/schedule.py with a fake database and repositories."""
import contextlib
import dataclasses
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.services import schedule
from bot.services.schedule import UNSET, RuleConflict, ScheduleDate, ScheduleService
from bot.services.schedule_rules import Rule
from bot.ui import texts
from tests.fake_events import fake_events
from tests.notion.factories import make_event

TODAY = datetime.date(2026, 6, 1)
D = datetime.date
GUILD = 7


class _FakeConn:
    """A connection whose transaction() and execute() do nothing."""

    def __init__(self) -> None:
        self.execute = AsyncMock()

    @contextlib.asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self):
        return self._tx()


class _FakeDb:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    @contextlib.asynccontextmanager
    async def _acquire(self):
        yield self.conn

    def acquire(self):
        return self._acquire()


@pytest.fixture
def world():
    """Events, manual ids, rules and defaults as the repositories return them."""
    state = SimpleNamespace(
        events=[], manual=set(), rules=[], defaults={}, config={}, inserted=[]
    )

    async def list_events(db, *, include_past=False):
        return [e for e in state.events if include_past or e.date >= TODAY]

    async def get_event(db, uid):
        return next((e for e in state.events if e.page_id == uid), None)

    async def update_event(db, uid, **fields):
        for i, e in enumerate(state.events):
            if e.page_id == uid:
                state.events[i] = dataclasses.replace(e, **fields)
                return True
        return False

    async def manual_ids(db, ids):
        return {i for i in ids if i in state.manual}

    async def list_rules(db, guild_id):
        return list(state.rules)

    async def insert_rule(db, guild_id, rule):
        new = dataclasses.replace(rule, id=len(state.rules) + 100)
        state.rules.append(new)
        state.inserted.append(new)
        return new

    async def get_default(db, guild_id, field):
        return state.defaults.get(field)

    async def get_value(db, guild_id, key):
        return state.config.get(key)

    fake_events.list_events = AsyncMock(side_effect=list_events)
    with patch("bot.services.schedule.local_today", return_value=TODAY), \
         patch("bot.services.schedule.db_events.list_events", new=list_events), \
         patch("bot.services.schedule.db_events.get_event", new=get_event), \
         patch("bot.services.schedule.db_events.update_event", new=update_event), \
         patch("bot.services.schedule.get_manual_page_ids", new=manual_ids), \
         patch("bot.services.schedule.db_rules.list_rules", new=list_rules), \
         patch("bot.services.schedule.db_rules.insert_rule", new=insert_rule), \
         patch("bot.services.schedule.db_rules.get_default", new=get_default), \
         patch("bot.services.schedule.get_value", new=get_value), \
         patch("bot.services.schedule.changed") as changed:
        state.changed = changed
        yield state


def _service() -> ScheduleService:
    ctx = SimpleNamespace(
        db=_FakeDb(), mirror=None, lineup_service=SimpleNamespace(refresh=AsyncMock())
    )
    return ScheduleService(ctx)


def _event(uid: str, day: D, **kw):
    return dataclasses.replace(
        make_event(event_id=f"{day.isoformat()} Rave Night", page_id=uid, date=day), **kw
    )


def test_label_uses_event_name_or_rave_night() -> None:
    ev = make_event(event_id="2026-12-05 Winter Rave", date=D(2026, 12, 5))
    assert ScheduleDate(D(2026, 12, 5), ev).label == "Winter Rave · 05.12.2026"
    assert ScheduleDate(D(2026, 12, 5), None).label == "Rave Night · 05.12.2026"


async def test_upcoming_dates_continue_grid_after_events(world) -> None:
    world.events = [_event("a", D(2026, 6, 6)), _event("m", D(2026, 6, 9))]
    world.manual = {"m"}
    dates = await _service().upcoming_dates(GUILD, 3)
    assert [d.date for d in dates] == [D(2026, 6, 6), D(2026, 6, 13), D(2026, 6, 20)]
    assert dates[0].event is not None and dates[1].event is None


async def test_upcoming_dates_skip_date_a_manual_event_id_holds(world) -> None:
    world.events = [_event("a", D(2026, 6, 6)), _event("m", D(2026, 6, 13))]
    world.manual = {"m"}
    dates = await _service().upcoming_dates(GUILD, 3, interval_days=0)
    assert [d.date for d in dates] == [D(2026, 6, 6), D(2026, 6, 7), D(2026, 6, 8)]
    dates = await _service().upcoming_dates(GUILD, 3)
    assert [d.date for d in dates] == [D(2026, 6, 6), D(2026, 6, 20), D(2026, 6, 27)]


async def test_upcoming_dates_start_at_first_date_without_events(world) -> None:
    dates = await _service().upcoming_dates(
        GUILD, 2, interval_days=14, first_date=D(2026, 6, 5)
    )
    assert [d.date for d in dates] == [D(2026, 6, 5), D(2026, 6, 19)]


async def test_add_rule_conflict_names_rules(world) -> None:
    world.events = [_event("a", D(2026, 6, 6))]
    world.rules = [
        Rule(1, "genre", "Techno", 2, D(2026, 6, 6)),
        Rule(2, "genre", "House", 2, D(2026, 6, 13)),
        Rule(3, "location", "Club", 1, D(2026, 6, 6)),
    ]
    with pytest.raises(RuleConflict) as err:
        await _service().add_rule(GUILD, Rule(None, "genre", "Trance", 1, D(2026, 6, 6)))
    assert [r.id for r in err.value.rules] == [1, 2]
    assert world.inserted == []


async def test_add_rule_inserts_when_free(world) -> None:
    world.events = [_event("a", D(2026, 6, 6))]
    world.rules = [Rule(1, "genre", "Techno", 2, D(2026, 6, 6))]
    added = await _service().add_rule(GUILD, Rule(None, "genre", " House ", 2, D(2026, 6, 13)))
    assert added.id is not None and added.value == "House"


async def test_add_rule_rejects_anchor_not_upcoming(world) -> None:
    world.events = [_event("a", D(2026, 6, 6))]
    svc = _service()
    for anchor in (D(2026, 6, 7), D(2026, 5, 30)):
        with pytest.raises(ValueError, match=texts.RULE_ANCHOR_NOT_UPCOMING):
            await svc.add_rule(GUILD, Rule(None, "genre", "Techno", 1, anchor))
    for every in (0, 53):
        with pytest.raises(ValueError, match=texts.RULE_EVERY_RANGE):
            await svc.add_rule(GUILD, Rule(None, "genre", "Techno", every, D(2026, 6, 6)))
    with pytest.raises(ValueError, match=texts.RULE_VALUE_EMPTY):
        await svc.add_rule(GUILD, Rule(None, "genre", "  ", 1, D(2026, 6, 6)))
    assert world.inserted == []


async def test_set_value_marks_hand(world) -> None:
    world.events = [_event("a", D(2026, 6, 6))]
    svc = _service()
    event = await svc.set_event_values(GUILD, "a", genre=" Techno ", note="Bring\nwater")
    assert event.genre == "Techno" and event.genre_by_hand is True
    assert event.notes == "Bring\nwater"
    assert event.location is None and event.location_by_hand is False
    world.changed.assert_called_once_with(svc.ctx, "event", "a")
    svc.ctx.lineup_service.refresh.assert_awaited_once()


async def test_clear_hand_value_falls_back_to_rule(world) -> None:
    world.events = [_event("a", D(2026, 6, 6), genre="Mine", genre_by_hand=True,
                           location="Here", location_by_hand=True)]
    world.rules = [Rule(1, "genre", "Techno", 1, D(2026, 6, 6))]
    world.defaults = {"location": "Club"}
    event = await _service().set_event_values(GUILD, "a", genre="", location=None)
    assert (event.genre, event.genre_by_hand) == ("Techno", False)
    assert (event.location, event.location_by_hand) == ("Club", False)


async def test_clear_on_manual_event_empties(world) -> None:
    world.events = [_event("m", D(2026, 6, 6), genre="Mine", genre_by_hand=True,
                           notes="x")]
    world.manual = {"m"}
    world.rules = [Rule(1, "genre", "Techno", 1, D(2026, 6, 6))]
    event = await _service().set_event_values(GUILD, "m", genre=None, note="")
    assert event.genre is None and event.genre_by_hand is False
    assert event.notes is None


async def test_set_values_unknown_event_raises_lookup(world) -> None:
    with pytest.raises(LookupError):
        await _service().set_event_values(GUILD, "nope", genre="x")


async def test_unset_leaves_fields_alone(world) -> None:
    world.events = [_event("a", D(2026, 6, 6), genre="G", genre_by_hand=True, notes="n")]
    event = await _service().set_event_values(GUILD, "a", genre=UNSET, location="L")
    assert (event.genre, event.notes, event.location) == ("G", "n", "L")


async def test_value_limits(world) -> None:
    world.events = [_event("a", D(2026, 6, 6))]
    svc = _service()
    cases = [
        ({"genre": "g" * 41}, texts.GENRE_TOO_LONG),
        ({"location": "l" * 81}, texts.LOCATION_TOO_LONG),
        ({"note": "n" * 301}, texts.NOTE_TOO_LONG),
        ({"location": "a\nb"}, texts.LOCATION_ONE_LINE),
        ({"genre": "a\nb"}, texts.GENRE_ONE_LINE),
    ]
    for kwargs, message in cases:
        with pytest.raises(ValueError, match=message):
            await svc.set_event_values(GUILD, "a", **kwargs)
    # Limits are inclusive; nothing above was written.
    assert world.events[0].genre is None
    event = await svc.set_event_values(
        GUILD, "a", genre="g" * 40, location="l" * 80, note="n" * 300
    )
    assert len(event.genre) == 40 and len(event.location) == 80 and len(event.notes) == 300


async def test_refresh_error_is_logged_not_raised(world) -> None:
    world.events = [_event("a", D(2026, 6, 6))]
    svc = _service()
    svc.ctx.lineup_service.refresh = AsyncMock(side_effect=RuntimeError("boom"))
    event = await svc.set_event_values(GUILD, "a", note="hi")
    assert event.notes == "hi"


async def test_interval_days_clamped(world) -> None:
    svc = _service()
    assert await svc.interval_days(GUILD) == 7
    world.config["event_interval_days"] = "0"
    assert await svc.interval_days(GUILD) == 1
    world.config["event_interval_days"] = "x"
    assert await svc.interval_days(GUILD) == 7


async def test_check_reanchor_reports_pairs(world) -> None:
    world.rules = [
        Rule(1, "genre", "Techno", 1, D(2026, 6, 6)),
        Rule(2, "genre", "House", 1, D(2026, 6, 9)),
        Rule(3, "location", "Club", 1, D(2026, 6, 6)),
    ]
    pairs = await _service().check_reanchor(GUILD, 7, D(2026, 6, 6))
    assert [(a.id, b.id) for a, b in pairs] == [(1, 2)]
    assert pairs[0][1].anchor == D(2026, 6, 13)


def test_module_exports() -> None:
    assert schedule.UNSET is UNSET
