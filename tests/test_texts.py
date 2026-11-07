"""Tests for bot/ui/texts.py."""
import datetime

from bot.ui import texts
from tests.notion.factories import make_event


def _event():
    return make_event(
        date=datetime.date(2026, 11, 14), start=datetime.time(18, 0), slot_minutes=60
    )


def test_fmt_date() -> None:
    assert texts.fmt_date(_event()) == "Saturday 14.11.2026"


def test_fmt_time() -> None:
    assert texts.fmt_time(_event(), 1) == "19:00"


def test_slot_phrase() -> None:
    assert texts.slot_phrase(_event(), 1) == "slot 2 at 19:00"


def _catalog_values() -> list[str]:
    event = _event()
    values: list[str] = []
    for name in dir(texts):
        if name.startswith("_") or not name.isupper():
            continue
        value = getattr(texts, name)
        if isinstance(value, dict):
            values.extend(value.values())
        else:
            values.append(value)
    values += [
        texts.invite_opener("EV"),
        texts.slot_button(event, 0),
        texts.event_line(1, event, 1),
        texts.event_line(2, event, 3),
        texts.booking_body(event, 0),
        texts.cancel_confirm(event, 0),
        texts.reminder(event, 0),
        texts.dm_booked(event, 0),
        texts.dm_cancelled(event, 0),
        texts.dm_added(event, 0),
        texts.dm_removed(event, 0),
    ]
    return values


def test_constants_non_empty_strings() -> None:
    values = _catalog_values()
    assert len(values) > 40
    assert all(isinstance(v, str) and v for v in values)


def test_no_em_dash() -> None:
    assert all("—" not in v for v in _catalog_values())


def test_formatted_texts() -> None:
    event = make_event(
        event_id="EV", date=datetime.date(2026, 11, 14), start=datetime.time(18, 0)
    )
    assert texts.slot_button(event, 0) == "Slot 1 · 18:00"
    assert texts.event_line(1, event, 1) == "1. EV · Saturday 14.11.2026 · 1 slot open"
    assert texts.event_line(2, event, 3).endswith("3 slots open")
    assert texts.booking_body(event, 0) == "EV\nSaturday 14.11.2026, slot 1 at 18:00"
    assert texts.cancel_confirm(event, 0) == "Cancel slot 1 (18:00) at EV?"
    assert texts.dm_booked(event, 0) == "An organizer booked you: EV, slot 1 at 18:00."
