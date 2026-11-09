"""Tests for bot/ui/setup_options.py."""
import datetime

import pytest

from bot.ui import setup_options as so

TODAY = datetime.date(2026, 11, 9)


@pytest.mark.parametrize(
    "key,current",
    [
        ("slots_per_event", 6),
        ("slots_per_event", 77),
        ("default_start_time", "20:00"),
        ("default_start_time", "03:15"),
        ("slot_duration_minutes", 60),
        ("slot_duration_minutes", 1000),
        ("event_interval_days", 14),
        ("event_interval_days", 10),
        ("first_event_date", ""),
        ("event_lookahead_count", 6),
        ("event_lookahead_count", 40),
        ("timezone", "Europe/Berlin"),
        ("timezone", "Pacific/Auckland"),
    ],
)
def test_dropdown_sizes_never_exceed_25(key, current):
    options = so.dropdown_options(key, current, TODAY)
    assert 1 <= len(options) <= 25
    assert len({value for _, value in options}) == len(options)


def test_stored_value_stays_selectable():
    values = [v for _, v in so.dropdown_options("event_interval_days", 10, TODAY)]
    assert "10" in values and {"7", "14", "21", "28"} <= set(values)


def test_stored_value_is_added_even_when_list_is_full():
    values = [v for _, v in so.dropdown_options("slots_per_event", 77, TODAY)]
    assert "77" in values and len(values) == 25


def test_first_date_options():
    options = so.dropdown_options("first_event_date", "", TODAY)
    assert options[0] == ("As soon as the interval allows", "asap")
    assert options[1] == ("Mon 09 Nov", "2026-11-09")
    assert len(options) == 25
    assert ("Sat 14 Nov", "2026-11-14") in options


def test_first_date_stored_value_beyond_range_is_kept():
    values = [v for _, v in so.dropdown_options("first_event_date", "2027-03-06", TODAY)]
    assert "2027-03-06" in values


@pytest.mark.parametrize(
    "key,raw,ok",
    [
        ("event_interval_days", "14", True),
        ("event_interval_days", " 14 ", True),
        ("event_interval_days", "0", False),
        ("event_interval_days", "366", False),
        ("event_interval_days", "x", False),
        ("slots_per_event", "100", True),
        ("slots_per_event", "101", False),
        ("slot_duration_minutes", "1440", True),
        ("slot_duration_minutes", "1441", False),
        ("event_lookahead_count", "52", True),
        ("event_lookahead_count", "53", False),
    ],
)
def test_parse_choice_numbers(key, raw, ok):
    out, errors = so.parse_choice(key, raw, TODAY)
    if ok:
        assert out == {key: int(raw.strip())} and errors == []
    else:
        assert out == {} and len(errors) == 1
        assert so.FIELD_LABELS[key].split(" (")[0] in errors[0]


def test_parse_choice_start_time():
    assert so.parse_choice("default_start_time", " 9:30 ", TODAY) == (
        {"default_start_time": "09:30"},
        [],
    )
    out, errors = so.parse_choice("default_start_time", "24:00", TODAY)
    assert out == {} and "Start time" in errors[0]


def test_parse_choice_first_date():
    assert so.parse_choice("first_event_date", "", TODAY) == ({"first_event_date": ""}, [])
    assert so.parse_choice("first_event_date", "2026-11-09", TODAY)[1] == []
    assert so.parse_choice("first_event_date", "2026-11-08", TODAY)[0] == {}
    assert so.parse_choice("first_event_date", "14.11.2026", TODAY)[0] == {}
    out, errors = so.parse_choice("first_event_date", "2026-11-14", TODAY)
    assert out == {"first_event_date": "2026-11-14"} and errors == []
    assert "First event date" in so.parse_choice("first_event_date", "bad", TODAY)[1][0]


def test_parse_choice_timezone():
    assert so.parse_choice("timezone", "Europe/Berlin", TODAY) == (
        {"timezone": "Europe/Berlin"},
        [],
    )
    out, errors = so.parse_choice("timezone", "Mars/Base", TODAY)
    assert out == {} and "Time zone" in errors[0]


def test_parse_hhmm():
    assert so.parse_hhmm("9:30") == "09:30" and so.parse_hhmm("23:59") == "23:59"
    assert so.parse_hhmm("24:00") is None and so.parse_hhmm("x") is None


def test_limits():
    assert so.LIMITS == {
        "slots_per_event": 100,
        "slot_duration_minutes": 1440,
        "event_interval_days": 365,
        "event_lookahead_count": 52,
    }


@pytest.mark.parametrize(
    "key,current",
    [
        ("slots_per_event", 6),
        ("default_start_time", "20:00"),
        ("slot_duration_minutes", 60),
        ("event_interval_days", 14),
        ("first_event_date", ""),
        ("first_event_date", "2026-12-24"),
        ("event_lookahead_count", 6),
        ("timezone", "Europe/Berlin"),
    ],
)
def test_option_values_fit_discord_limits(key, current):
    options = so.dropdown_options(key, current, TODAY)
    assert all(1 <= len(value) <= 100 and 1 <= len(label) <= 100 for label, value in options)


def test_asap_option_has_a_non_empty_value_and_parses_to_empty_date():
    label, value = so.dropdown_options("first_event_date", "", TODAY)[0]
    assert label == so.ASAP_LABEL and value == so.ASAP_VALUE != ""
    assert so.parse_choice("first_event_date", so.ASAP_VALUE, TODAY) == (
        {"first_event_date": ""},
        [],
    )
