"""Tests for bot/services/announcement_template.py."""
import dataclasses
import datetime

from bot.notion.models import SlotInfo
from bot.services import announcement_template as at

UTC = datetime.UTC


def _event(slots=3, booked=(1, 2, 3), **kw):
    sample = at.sample_event()
    rows = tuple(
        SlotInfo(f"s{n}", n, f"d{n}" if n in booked else None,
                 f"DJ {n}" if n in booked else None, None)
        for n in range(1, slots + 1)
    )
    base = dict(slot_count=slots, slots=rows, event_id="Rave One", notes="Bring ear plugs",
                genre="Techno", location="Hall")
    base.update(kw)
    return dataclasses.replace(sample, **base)


def _ts(dt):
    return int(dt.timestamp())


def test_render_fills_every_placeholder():
    ev = _event(slots=3, booked=(1, 3))
    s0 = ev.slot_start(0)
    text = at.render(
        "{title}|{date}|{countdown}|{description}|{notes}|{slots}|{booked}|{lineup}",
        ev, at.GENERAL, s0 - datetime.timedelta(days=1),
    )
    parts = text.split("|")
    assert parts[0] == "Rave One"
    assert parts[1] == f"<t:{_ts(s0)}:F>"
    assert parts[2] == f"<t:{_ts(s0)}:R>"
    assert parts[3] == parts[4] == "Bring ear plugs"
    assert parts[5:7] == ["3", "2"]
    lines = parts[7].split("\n")
    assert lines == [
        f"<t:{_ts(ev.slot_start(0))}:t> DJ 1",
        f"<t:{_ts(ev.slot_start(1))}:t> \U0001f7e2 *open slot*",
        f"<t:{_ts(ev.slot_start(2))}:t> DJ 3",
    ]


def test_empty_values_leave_no_blank_label_line():
    ev = _event(genre=None)
    now = ev.slot_start(0)
    text = at.render("Genre: {genre}\nPlace: {location} {genre}\nEnd", ev, at.GENERAL, now)
    assert text == "Place: Hall \nEnd"


def test_values_are_escaped():
    ev = _event()
    slots = (SlotInfo("s1", 1, "d1", "@everyone <@&5> **x**", None),)
    ev = dataclasses.replace(ev, slot_count=1, slots=slots)
    text = at.render("<@&123> {lineup}", ev, at.GENERAL, ev.slot_start(0))
    assert text.startswith("<@&123> ")
    assert "<@&5>" not in text
    assert "**x**" not in text
    assert "@everyone" not in text


def test_doubled_braces_are_literal():
    ev = _event()
    assert at.render("{{title}} {title}", ev, at.GENERAL, ev.slot_start(0)) == "{title} Rave One"


def test_unknown_placeholder_stays_as_typed():
    ev = _event()
    assert at.render("{oops}", ev, at.GENERAL, ev.slot_start(0)) == "{oops}"


def test_now_playing_and_next_up():
    ev = _event(slots=3)
    mid = ev.slot_start(1) + datetime.timedelta(minutes=5)
    tpl = "{now_playing}|{next_up}|{status}"
    assert at.render(tpl, ev, at.START, mid) == "DJ 2|DJ 3|Live now."
    after = at.event_end(ev) + datetime.timedelta(minutes=1)
    text = at.render("N:{now_playing}\nX:{next_up}\n{status}", ev, at.START, after)
    assert text == "The event has ended."
    last = ev.slot_start(2) + datetime.timedelta(minutes=1)
    assert at.render("{next_up}|{now_playing}", ev, at.START, last) == "|DJ 3"
    before = ev.slot_start(0) - datetime.timedelta(hours=1)
    assert at.render("{now_playing}|{next_up}", ev, at.START, before) == "|DJ 1"


def test_event_end_is_last_slot_start_plus_slot_length():
    ev = _event(slots=3)
    assert at.event_end(ev) == ev.slot_start(2) + datetime.timedelta(minutes=ev.slot_minutes)
    assert at.current_slot_index(ev, at.event_end(ev)) is None
    assert at.current_slot_index(ev, ev.slot_start(1)) == 1


def test_check_flags_unknown_placeholder_and_unbalanced_braces():
    res = at.check_template("{title} {nope}", at.GENERAL, None)
    assert not res.ok
    assert any("{nope}" in p for p in res.problems)
    res = at.check_template("{title", at.GENERAL, None)
    assert not res.ok
    assert any("braces" in p for p in res.problems)
    assert not at.check_template("oops }", at.GENERAL, None).ok


def test_check_over_limit_reports_length():
    res = at.check_template("x" * 2001, at.GENERAL, None)
    assert not res.ok
    assert res.length == 2001
    assert any("2001" in p for p in res.problems)


def test_check_warns_for_everyone_and_here():
    res = at.check_template("@everyone and @here {title}", at.GENERAL, None)
    assert res.ok
    assert len(res.warnings) == 2
    assert all("will not ping" in w for w in res.warnings)


def test_check_flags_malformed_role_mention():
    assert not at.check_template("<@&abc>", at.GENERAL, None).ok
    assert at.check_template("<@&123>", at.GENERAL, None).ok


def test_check_uses_sample_event_without_a_real_one():
    res = at.check_template("{title}: {lineup}", at.GENERAL, None)
    assert res.ok
    assert at.sample_event().event_id in res.preview
    assert "open slot" in res.preview
    assert res.length == len(res.preview)


def test_check_empty_template_is_ok():
    res = at.check_template("", at.GENERAL, None)
    assert res.ok
    assert res.preview == ""


def test_default_templates_are_valid():
    for kind, template in at.DEFAULT_TEMPLATES.items():
        res = at.check_template(template, kind, None)
        assert res.ok, res.problems
        assert not res.warnings
