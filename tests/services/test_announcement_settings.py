"""Tests for bot/services/announcement_settings.py (in-memory fakes, no database)."""
import dataclasses
import datetime

import pytest

from bot.db import announcements, config
from bot.services import announcement_settings as s
from bot.services import announcement_template as at

UTC = datetime.UTC
G = 1


@pytest.fixture
def store(monkeypatch):
    values: dict[tuple[int, str], str] = {}
    rules: list[announcements.AnnouncementRule] = []

    async def get_value(db, guild_id, key):
        return values.get((guild_id, key))

    async def set_value(db, guild_id, key, value):
        values[(guild_id, key)] = value

    async def delete_value(db, guild_id, key):
        values.pop((guild_id, key), None)

    async def list_rules(db, guild_id):
        return list(rules)

    async def add_rule(db, guild_id, days_before, post_time):
        rule = announcements.AnnouncementRule(
            len(rules) + 1, days_before, post_time, datetime.datetime.now(UTC)
        )
        rules.append(rule)
        return rule

    monkeypatch.setattr(config, "get_value", get_value)
    monkeypatch.setattr(config, "set_value", set_value)
    monkeypatch.setattr(config, "delete_value", delete_value)
    monkeypatch.setattr(announcements, "list_rules", list_rules)
    monkeypatch.setattr(announcements, "add_rule", add_rule)
    return values, rules


def _rule(days=2, time=datetime.time(18, 0), created=None):
    created = created or datetime.datetime(2020, 1, 1, tzinfo=UTC)
    return announcements.AnnouncementRule(1, days, time, created)


def _event():
    return dataclasses.replace(
        at.sample_event(), date=datetime.date(2027, 3, 29), start=datetime.time(21, 0)
    )


def _settings(since=None):
    return s.AnnouncementSettings(None, True, True, "g", "s", since)


async def test_enable_general_creates_default_rule_once(store):
    values, rules = store
    await s.set_enabled(None, G, "general", True)
    assert [(r.days_before, r.post_time) for r in rules] == [(3, datetime.time(18, 0))]
    await s.set_enabled(None, G, "general", False)
    await s.set_enabled(None, G, "general", True)
    assert len(rules) == 1
    rules.clear()
    existing = await announcements.add_rule(None, G, 1, datetime.time(9, 0))
    await s.set_enabled(None, G, "general", True)
    assert rules == [existing]


async def test_enable_stores_since_and_disable_keeps_rules(store):
    values, rules = store
    now = datetime.datetime(2027, 1, 1, 12, 0, tzinfo=UTC)
    await s.set_enabled(None, G, "general", True, now)
    assert values[(G, s.ANNOUNCE_GENERAL_SINCE_KEY)] == now.isoformat()
    assert values[(G, s.ANNOUNCE_GENERAL_KEY)] == "true"
    await s.set_enabled(None, G, "general", False)
    assert values[(G, s.ANNOUNCE_GENERAL_KEY)] == "false"
    assert values[(G, s.ANNOUNCE_GENERAL_SINCE_KEY)] == now.isoformat()
    assert len(rules) == 1
    loaded = await s.load_settings(None, G)
    assert loaded.general_since == now and not loaded.general_enabled


async def test_unknown_kind_raises(store):
    with pytest.raises(ValueError):
        await s.set_enabled(None, G, "bogus", True)
    with pytest.raises(ValueError):
        await s.save_template(None, G, "bogus", "x")


async def test_set_channel_and_load(store):
    await s.set_channel(None, G, 42)
    assert (await s.load_settings(None, G)).channel_id == 42
    await s.set_channel(None, G, None)
    assert (await s.load_settings(None, G)).channel_id is None


async def test_empty_template_loads_default(store):
    values, _ = store
    loaded = await s.load_settings(None, G)
    assert loaded.general_template == at.DEFAULT_TEMPLATES[at.GENERAL]
    assert loaded.start_template == at.DEFAULT_TEMPLATES[at.START]
    values[(G, s.ANNOUNCE_GENERAL_TEMPLATE_KEY)] = "   "
    loaded = await s.load_settings(None, G)
    assert loaded.general_template == at.DEFAULT_TEMPLATES[at.GENERAL]


async def test_save_template_stores_and_empty_deletes(store):
    values, _ = store
    check = await s.save_template(None, G, "general", "Hello {title}")
    assert check.ok
    assert values[(G, s.ANNOUNCE_GENERAL_TEMPLATE_KEY)] == "Hello {title}"
    check = await s.save_template(None, G, "general", "  ")
    assert check.ok
    assert (G, s.ANNOUNCE_GENERAL_TEMPLATE_KEY) not in values


async def test_save_template_refuses_an_invalid_template_and_keeps_the_old_one(store):
    values, _ = store
    await s.save_template(None, G, "start", "Old {title}")
    check = await s.save_template(None, G, "start", "Bad {nope}")
    assert not check.ok
    assert values[(G, s.ANNOUNCE_START_TEMPLATE_KEY)] == "Old {title}"
    too_long = "x" * 2500
    long_event = _event()
    check = await s.save_template(None, G, "start", too_long, long_event)
    assert not check.ok
    assert values[(G, s.ANNOUNCE_START_TEMPLATE_KEY)] == "Old {title}"


async def test_save_template_checks_the_given_event(store, monkeypatch):
    values, _ = store
    real = at.check_template

    def fake(text, kind, event):
        ok = event is None
        return dataclasses.replace(real(text, kind, None), ok=ok)

    monkeypatch.setattr(s, "check_template", fake)
    check = await s.save_template(None, G, "general", "Hi", _event())
    assert not check.ok
    assert (G, s.ANNOUNCE_GENERAL_TEMPLATE_KEY) not in values


def test_rule_post_time_is_wall_clock_across_dst():
    post = s.rule_post_time(_event(), _rule(days=2))
    assert post.replace(tzinfo=None) == datetime.datetime(2027, 3, 27, 18, 0)
    assert post.utcoffset() == datetime.timedelta(hours=1)
    assert _event().slot_start(0).utcoffset() == datetime.timedelta(hours=2)


def test_rule_due_window():
    ev, rule = _event(), _rule()
    post = s.rule_post_time(ev, rule)
    eps = datetime.timedelta(seconds=1)
    assert not s.is_rule_due(ev, rule, _settings(), post - eps)
    assert s.is_rule_due(ev, rule, _settings(), post)
    assert s.is_rule_due(ev, rule, _settings(), ev.slot_start(0) - eps)
    assert not s.is_rule_due(ev, rule, _settings(), ev.slot_start(0))
    assert not s.is_rule_due(ev, rule, _settings(), ev.slot_start(0) + eps)


def test_rule_created_after_its_post_time_is_not_due():
    ev = _event()
    post = s.rule_post_time(ev, _rule())
    now = post + datetime.timedelta(hours=1)
    late = _rule(created=post + datetime.timedelta(minutes=1))
    assert not s.is_rule_due(ev, late, _settings(), now)
    early = _rule(created=post - datetime.timedelta(minutes=1))
    assert s.is_rule_due(ev, early, _settings(), now)
    assert not s.is_rule_due(ev, _rule(created=post), _settings(), now)


def test_switch_on_after_post_time_is_not_due():
    ev, rule = _event(), _rule()
    post = s.rule_post_time(ev, rule)
    now = post + datetime.timedelta(hours=1)
    assert not s.is_rule_due(ev, rule, _settings(since=post + datetime.timedelta(minutes=5)), now)
    assert s.is_rule_due(ev, rule, _settings(since=post - datetime.timedelta(minutes=5)), now)
