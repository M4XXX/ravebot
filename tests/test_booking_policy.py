"""Tests for bot/policy.py (booking rules core)."""
import datetime
from types import SimpleNamespace

import pytest

from bot import policy
from bot.db import config as config_db
from bot.policy import (
    DEFAULT_RULES,
    BookingRules,
    at_slot_limit,
    cancellation_closed,
    cancellation_closed_text,
    current_rules,
    describe_minutes,
    load_rules,
    no_event_below_limit_text,
    parse_rules_form,
    rules_summary,
    save_rules,
    slot_limit_text,
    slots_held,
    utc_now,
)
from tests.notion.factories import make_event, make_slot

GUILD = 77
EM_DASH = "—"


@pytest.fixture
def store(monkeypatch):
    data: dict[tuple[int, str], str] = {}

    async def get_value(pool, guild_id, key):
        return data.get((guild_id, key))

    async def set_value(pool, guild_id, key, value):
        data[(guild_id, key)] = value

    monkeypatch.setattr(config_db, "get_value", get_value)
    monkeypatch.setattr(config_db, "set_value", set_value)
    return data


def _put(store, **values):
    for key, value in values.items():
        store[(GUILD, key)] = value


async def test_defaults_when_missing(store):
    assert await load_rules(object(), GUILD) == DEFAULT_RULES
    assert DEFAULT_RULES == BookingRules(True, 2, True, 60)
    assert len(policy.RULE_KEYS) == 4


async def test_none_db_or_guild_gives_defaults(store):
    _put(store, **dict(zip(policy.RULE_KEYS, ["false", "5", "false", "0"])))
    assert await load_rules(None, GUILD) == DEFAULT_RULES
    assert await load_rules(object(), None) == DEFAULT_RULES


async def test_load_stored_values(store):
    k = policy.RULE_KEYS
    _put(store, **{k[0]: "false", k[1]: "5", k[2]: "false", k[3]: "0"})
    assert await load_rules(object(), GUILD) == BookingRules(False, 5, False, 0)


@pytest.mark.parametrize(
    ("idx", "value", "field", "expected"),
    [
        (1, "abc", "slot_limit_max", 2),
        (1, "0", "slot_limit_max", 2),
        (1, "101", "slot_limit_max", 2),
        (1, "1.5", "slot_limit_max", 2),
        (3, "abc", "cancel_cutoff_minutes", 60),
        (3, "10081", "cancel_cutoff_minutes", 60),
        (3, "-1", "cancel_cutoff_minutes", 60),
        (3, "10080", "cancel_cutoff_minutes", 10080),
        (1, "100", "slot_limit_max", 100),
    ],
)
async def test_load_out_of_range_falls_back(store, idx, value, field, expected):
    _put(store, **{policy.RULE_KEYS[idx]: value})
    assert getattr(await load_rules(object(), GUILD), field) == expected


async def test_load_never_raises(monkeypatch):
    async def boom(pool, guild_id, key):
        raise RuntimeError("db down")

    monkeypatch.setattr(config_db, "get_value", boom)
    assert await load_rules(object(), GUILD) == DEFAULT_RULES


async def test_save_load_round_trip(store):
    rules = BookingRules(False, 7, True, 15)
    await save_rules(object(), GUILD, rules)
    assert len(store) == 4
    assert await load_rules(object(), GUILD) == rules


async def test_current_rules(store):
    await save_rules(object(), GUILD, BookingRules(True, 3, False, 60))
    bot = SimpleNamespace(db=object(), guilds=[SimpleNamespace(id=GUILD)])
    assert (await current_rules(bot.db, bot)).slot_limit_max == 3
    assert await current_rules(bot.db, None) == DEFAULT_RULES
    empty = SimpleNamespace(guilds=[])
    assert await current_rules(bot.db, empty) == DEFAULT_RULES


def test_parse_valid():
    rules, errors = parse_rules_form(
        {
            "slot_limit_enabled": "on",
            "slot_limit_max": "3",
            "cancel_cutoff_enabled": "on",
            "cancel_cutoff_minutes": "90",
        }
    )
    assert errors == []
    assert rules == BookingRules(True, 3, True, 90)


def test_parse_unchecked():
    rules, errors = parse_rules_form(
        {"slot_limit_max": "3", "cancel_cutoff_minutes": "0"}
    )
    assert errors == []
    assert rules == BookingRules(False, 3, False, 0)


@pytest.mark.parametrize("bad", ["", " ", "-1", "1.5", "abc", "0", "101"])
def test_parse_bad_limit(bad):
    rules, errors = parse_rules_form({"slot_limit_max": bad, "cancel_cutoff_minutes": "60"})
    assert rules is None
    assert len(errors) == 1


@pytest.mark.parametrize("bad", ["", "-1", "1.5", "10081", "x"])
def test_parse_bad_cutoff(bad):
    rules, errors = parse_rules_form({"slot_limit_max": "2", "cancel_cutoff_minutes": bad})
    assert rules is None
    assert len(errors) == 1


def test_parse_missing_keys_and_both_errors():
    rules, errors = parse_rules_form({})
    assert rules is None
    assert len(errors) == 2


def _event(*owners):
    slots = [
        make_slot(i + 1, dj_page_id=f"dj{o}" if o else None, discord_id=o)
        for i, o in enumerate(owners)
    ]
    return make_event(slot_count=len(slots), slots=slots)


def test_slots_held_counts_only_user_with_dj():
    ev = _event(1, 2, 1, None, 1)
    assert slots_held(ev, 1) == 3
    assert slots_held(ev, 2) == 1
    assert slots_held(ev, 9) == 0
    unlinked = make_event(slots=[make_slot(1, dj_page_id=None, discord_id=1)])
    assert slots_held(unlinked, 1) == 0


def test_slots_held_uses_resolved_slots():
    ev = make_event(
        slots=[
            make_slot(1, dj_page_id="a", discord_id=1, page_id="p1"),
            make_slot(1, dj_page_id="a", discord_id=1, page_id="p2"),
        ]
    )
    assert slots_held(ev, 1) == 1


def test_at_slot_limit():
    rules = BookingRules(True, 2, True, 60)
    assert not at_slot_limit(_event(1), 1, rules)
    assert at_slot_limit(_event(1, 1), 1, rules)
    assert at_slot_limit(_event(1, 1, 1), 1, rules)
    assert not at_slot_limit(_event(1, 1, 1), 1, BookingRules(False, 2, True, 60))


def test_cancellation_closed_boundaries():
    ev = make_event()
    start = ev.slot_start(0)
    rules = BookingRules(True, 2, True, 60)
    cutoff = start - datetime.timedelta(minutes=60)
    assert cancellation_closed(ev, cutoff, rules)
    assert not cancellation_closed(ev, cutoff - datetime.timedelta(seconds=1), rules)
    zero = BookingRules(True, 2, True, 0)
    assert cancellation_closed(ev, start, zero)
    assert not cancellation_closed(ev, start - datetime.timedelta(seconds=1), zero)
    off = BookingRules(True, 2, False, 60)
    assert not cancellation_closed(ev, start + datetime.timedelta(hours=5), off)


def test_cancellation_closed_unreadable_start():
    ev = make_event()

    def boom(index):
        raise ValueError("bad")

    object.__setattr__(ev, "slot_start", boom)
    now = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    assert not cancellation_closed(ev, now, DEFAULT_RULES)


def test_utc_now_aware():
    assert utc_now().utcoffset() == datetime.timedelta(0)


@pytest.mark.parametrize(
    ("minutes", "text"),
    [
        (0, "the start of the event"),
        (60, "1 hour"),
        (120, "2 hours"),
        (90, "90 minutes"),
        (1, "1 minute"),
        (45, "45 minutes"),
    ],
)
def test_describe_minutes(minutes, text):
    assert describe_minutes(minutes) == text


@pytest.mark.parametrize("n", [1, 3, 17])
def test_texts_use_number(n):
    for text in (slot_limit_text(n), no_event_below_limit_text(n)):
        assert str(n) in text
        assert EM_DASH not in text


@pytest.mark.parametrize("m", [0, 30, 45, 90, 180])
def test_cancellation_text(m):
    text = cancellation_closed_text(m)
    assert describe_minutes(m) in text
    assert EM_DASH not in text


def test_rules_summary():
    assert rules_summary(DEFAULT_RULES) == [
        "Slot limit: on, 2 per event",
        "Cancellation cutoff: on, 60 minutes before the start",
    ]
    assert rules_summary(BookingRules(False, 2, False, 60)) == [
        "Slot limit: off",
        "Cancellation cutoff: off",
    ]
