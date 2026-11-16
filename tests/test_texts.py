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
    assert texts.booking_body(event, 0) == "EV\nSaturday 14.11.2026, slot 1 at 18:00"
    assert texts.cancel_confirm(event, 0) == "Cancel slot 1 (18:00) at EV?"
    assert texts.dm_booked(event, 0) == "An organizer booked you: EV, slot 1 at 18:00."


# --- /cancel event -----------------------------------------------------------


def _rave():
    return make_event(
        event_id="2026-11-28 Rave Night",
        date=datetime.date(2026, 11, 28),
        start=datetime.time(22, 0),
        slot_minutes=30,
        slot_count=4,
    )


def test_dm_event_canceled_lists_every_slot() -> None:
    assert texts.dm_event_canceled(_rave(), [0, 2]) == (
        "The event 2026-11-28 Rave Night was canceled by an organizer. "
        "Your booking (slot 1 at 22:00, slot 3 at 23:00) is canceled too."
    )


def test_dj_channel_event_canceled() -> None:
    assert texts.dj_channel_event_canceled(_rave()) == (
        "**2026-11-28 Rave Night** on Saturday 28.11.2026 is canceled."
    )


def test_dj_line() -> None:
    assert texts.dj_line("DJ Alpha", _rave(), [0, 2]) == (
        "- DJ Alpha, slot 1 at 22:00, slot 3 at 23:00"
    )


def test_log_event_canceled_text_without_and_with_djs() -> None:
    event = _rave()
    assert texts.log_event_canceled_text("Max", event, []) == (
        "Max canceled **2026-11-28 Rave Night**.\nNo DJ was booked."
    )
    line = texts.dj_line("DJ Alpha", event, [0])
    assert texts.log_event_canceled_text("Max", event, [line]) == (
        "Max canceled **2026-11-28 Rave Night**.\nBooked DJs:\n- DJ Alpha, slot 1 at 22:00"
    )


def test_cancel_event_confirm_without_bookings() -> None:
    assert texts.cancel_event_confirm(_rave(), [], []) == (
        "Cancel the event **2026-11-28 Rave Night**?\nNo DJ is booked."
    )


def test_cancel_event_confirm_lists_djs_and_by_hand_names() -> None:
    event = _rave()
    text = texts.cancel_event_confirm(event, [texts.dj_line("DJ Alpha", event, [0])], ["Guesty"])
    assert text == (
        "Cancel the event **2026-11-28 Rave Night**?\n"
        "These DJs are booked and will be told by DM:\n"
        "- DJ Alpha, slot 1 at 22:00\n"
        "Without a Discord account, tell them yourself: Guesty"
    )


def test_cancel_event_confirm_with_only_by_hand_djs() -> None:
    text = texts.cancel_event_confirm(_rave(), [], ["Guesty"])
    assert "No DJ is booked." not in text
    assert "will be told by DM" not in text
    assert text.endswith("Without a Discord account, tell them yourself: Guesty")


def test_cancel_event_confirm_caps_at_15_lines() -> None:
    event = _rave()
    lines = [texts.dj_line(f"DJ {'x' * 80} {i}", event, [0]) for i in range(20)]
    by_hand = [f"Guest {'y' * 80} {i}" for i in range(40)]
    text = texts.cancel_event_confirm(event, lines, by_hand)
    assert text.count("\n- ") == 15
    assert "and 5 more" in text
    assert len(text) < 2000


def test_event_cancel_done_counts() -> None:
    event = _rave()
    assert texts.event_cancel_done(event, [], [], []) == "Canceled 2026-11-28 Rave Night."
    assert texts.event_cancel_done(event, ["A"], [], []) == (
        "Canceled 2026-11-28 Rave Night. 1 DJ told by DM."
    )
    assert texts.event_cancel_done(event, ["A", "B"], ["Guesty", "C"], [1]) == (
        "Canceled 2026-11-28 Rave Night. 2 DJs told by DM. "
        "Tell these DJs yourself: Guesty, C. "
        "Some slots could not be emptied in Notion, see the log channel."
    )


def test_event_cancel_cleanup_failed_uses_slot_numbers() -> None:
    assert texts.event_cancel_cleanup_failed(_rave(), [0, 2]) == (
        "**2026-11-28 Rave Night** was canceled, but these slots could not be emptied "
        "in Notion: 1, 3."
    )


def test_cancel_event_names_are_escaped() -> None:
    event = _rave()
    evil = "**DJ** @everyone <@1>"
    for text in (
        texts.dj_line(evil, event, [0]),
        texts.tell_canceled_todo(evil),
        texts.event_canceled_dm_failed(evil, event),
        texts.event_canceled_no_discord(evil, event),
        texts.cancel_event_confirm(event, [], [evil]),
        texts.event_cancel_done(event, [], [evil], []),
    ):
        assert "**DJ**" not in text
        assert "@everyone" not in text
        assert "<@1>" not in text


def test_cancel_event_texts_have_no_em_dash() -> None:
    event = _rave()
    built = [
        texts.dm_event_canceled(event, [0]),
        texts.dj_channel_event_canceled(event),
        texts.log_event_canceled_text("Max", event, []),
        texts.cancel_event_confirm(event, [], []),
        texts.event_cancel_done(event, ["A"], ["B"], [0]),
        texts.event_cancel_cleanup_failed(event, [0]),
        texts.event_canceled_dm_failed("A", event),
        texts.event_canceled_no_discord("A", event),
        texts.tell_canceled_todo("A"),
    ]
    assert all("—" not in t for t in built)
    for name in (
        "LOG_EVENT_CANCELED_TITLE", "EVENT_CANCELED_ANYWAY", "EVENT_CANCEL_CLEANUP_TODO",
        "EVENT_CANCEL_POST_FAILED", "EVENT_CANCEL_KEPT", "EVENT_NOT_CANCELABLE",
        "EVENT_CANCEL_NOTION_FAILED", "EVENT_WAS_CANCELED",
    ):
        assert isinstance(getattr(texts, name), str) and getattr(texts, name)
    assert texts.LOG_EVENT_CANCELED_TITLE == "Event canceled"
