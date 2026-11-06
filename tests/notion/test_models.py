"""Tests for typed Notion models (hand-written fixtures from documented shapes)."""
import copy
import datetime
import json
from pathlib import Path

import pytest

from bot.notion.models import DjInfo, SlotInfo, parse_dj, parse_event, parse_slot

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / f"{name}_doc.json").read_text())


def event_with(**props) -> dict:
    page = load("event")
    for k, v in props.items():
        page["properties"][k.replace("_", " ")] = v
    return page


def test_valid_event_parses():
    ev = parse_event(load("event"))
    assert ev is not None
    assert ev.page_id == "evt-page-1"
    assert ev.event_id == "2026-11-14 Rave Night"
    assert ev.date == datetime.date(2026, 11, 14)
    assert ev.status == "Planning stage"
    assert ev.genre == "Techno"
    assert ev.notes is None
    assert ev.slot_count == 6
    assert ev.start == datetime.time(18, 0)
    assert ev.slot_minutes == 60
    assert ev.slots == ()


def test_slot_start_second_slot():
    ev = parse_event(load("event"))
    assert ev.slot_start(1) == datetime.datetime(2026, 11, 14, 19, 0, tzinfo=datetime.UTC)


def test_slot_start_wraps_past_midnight():
    page = load("event")
    page["properties"]["Slot length"]["number"] = 90
    ev = parse_event(page)
    assert ev.slot_start(3) == datetime.datetime(2026, 11, 14, 22, 30, tzinfo=datetime.UTC)
    assert ev.slot_start(5) == datetime.datetime(2026, 11, 15, 1, 30, tzinfo=datetime.UTC)


@pytest.mark.parametrize("value", ["8pm", "", "25:00", "18:60", "1800"])
def test_bad_start_returns_none(value, caplog):
    page = load("event")
    page["properties"]["Start"]["rich_text"] = [{"plain_text": value}] if value else []
    assert parse_event(page) is None
    assert "Start" in caplog.text


@pytest.mark.parametrize("n", [None, 0, -2])
def test_bad_slots_returns_none(n):
    page = load("event")
    page["properties"]["Slots"]["number"] = n
    assert parse_event(page) is None


@pytest.mark.parametrize("n", [None, 0, -5])
def test_bad_slot_length_returns_none(n):
    page = load("event")
    page["properties"]["Slot length"]["number"] = n
    assert parse_event(page) is None


def test_missing_or_empty_id_returns_none():
    page = load("event")
    page["properties"]["ID"]["title"] = []
    assert parse_event(page) is None
    del page["properties"]["ID"]
    assert parse_event(page) is None


def test_missing_date_returns_none():
    page = load("event")
    page["properties"]["Date"]["date"] = None
    assert parse_event(page) is None


def test_slot_with_dj():
    dj = parse_dj(load("dj"))
    slot = parse_slot(load("slot"), {dj.page_id: dj})
    assert slot == SlotInfo("slot-page-1", 2, "dj-page-1", "DJ Test", 123456789012345678)


def test_slot_open_and_unknown_dj():
    page = load("slot")
    page["properties"]["DJ"]["relation"] = []
    s = parse_slot(page, {})
    assert (s.dj_page_id, s.dj_name, s.discord_id) == (None, None, None)
    page = copy.deepcopy(load("slot"))
    s = parse_slot(page, {})
    assert s.dj_page_id == "dj-page-1" and s.dj_name is None


def test_dj_parses():
    dj = parse_dj(load("dj"))
    assert dj == DjInfo("dj-page-1", "DJ Test", 123456789012345678, "djtest")


def test_dj_without_discord_id():
    page = load("dj")
    page["properties"]["Discord ID"]["rich_text"] = []
    assert parse_dj(page).discord_id is None


def test_dj_non_digit_discord_id_is_none():
    page = load("dj")
    page["properties"]["Discord ID"]["rich_text"] = [{"plain_text": "abc"}]
    assert parse_dj(page).discord_id is None


def test_second_dj_lands_in_extra_dj_page_ids():
    page = load("slot")
    page["properties"]["DJ"]["relation"] = [{"id": "dj-page-1"}, {"id": "dj-2"}, {"id": "dj-3"}]
    slot = parse_slot(page, {})
    assert slot.dj_page_id == "dj-page-1"
    assert slot.extra_dj_page_ids == ("dj-2", "dj-3")
    assert parse_slot(load("slot"), {}).extra_dj_page_ids == ()
