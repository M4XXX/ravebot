"""Tests for bot/ui/texts.py."""
import datetime
import re

from bot.ui import texts
from tests.notion.factories import make_event


def _event():
    return make_event(
        date=datetime.date(2026, 11, 14), start=datetime.time(18, 0), slot_minutes=60
    )


def test_fmt_date() -> None:
    assert texts.fmt_date(_event()) == "Saturday 14.11.2026"


def test_plain_time() -> None:
    assert texts.plain_time(_event(), 1) == "19:00"


def test_slot_phrase() -> None:
    assert re.fullmatch(r"slot 2 at <t:\d+:t>", texts.slot_phrase(_event(), 1))


def test_slot_button_has_no_time() -> None:
    assert texts.slot_button(1) == "Slot 2"


def test_slot_time_is_discord_timestamp() -> None:
    assert re.fullmatch(r"<t:\d+:t>", texts.slot_time(_event(), 0))
    assert re.fullmatch(r"<t:\d+:F>", texts.slot_when(_event(), 0))


def test_event_meta() -> None:
    assert texts.event_meta(make_event(genre="Techno", location="Club Vortex")) == (
        "\U0001f3b5 Techno \u00b7 \U0001f4cd Club Vortex"
    )
    assert texts.event_meta(make_event(location="Club")) == "\U0001f4cd Club"
    assert texts.event_meta(make_event()) is None


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
        texts.slot_button(0),
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
    when = texts.slot_when(event, 0)
    assert texts.booking_body(event, 0) == f"EV\nSlot 1, {when}"
    assert texts.cancel_confirm(event, 0) == f"Cancel slot 1 ({when}) at EV?"
    assert texts.dm_booked(event, 0) == f"An organizer booked you: EV, slot 1, {when}."


def test_dj_texts_use_the_full_local_slot_time_and_no_bot_zone_date() -> None:
    """A slot after midnight for the DJ: no bot-zone date next to a local time."""
    event = make_event(
        event_id="2026-11-14 Rave Night",
        date=datetime.date(2026, 11, 14),
        start=datetime.time(23, 0),
        slot_minutes=60,
    )
    when = texts.slot_when(event, 1)
    for text in (
        texts.booking_body(event, 1),
        texts.reminder(event, 1),
        texts.cancel_confirm(event, 1),
        texts.dm_booked(event, 1),
        texts.dm_cancelled(event, 1),
        texts.dm_added(event, 1),
        texts.dm_removed(event, 1),
    ):
        assert when in text and "Rave Night" in text
        assert "2026-11-14" not in text and "14.11.2026" not in text
        assert texts.slot_time(event, 1) not in text


# --- /cancel event -----------------------------------------------------------


def _rave():
    return make_event(
        event_id="2026-11-28 Rave Night",
        date=datetime.date(2026, 11, 28),
        start=datetime.time(22, 0),
        slot_minutes=30,
        slot_count=4,
    )


def _t(index: int) -> str:
    return texts.slot_time(_rave(), index)


def _w(index: int) -> str:
    return texts.slot_when(_rave(), index)


def test_dm_event_canceled_lists_every_slot() -> None:
    assert texts.dm_event_canceled(_rave(), [0, 2]) == (
        "The event Rave Night was canceled by an organizer. "
        f"Your booking (slot 1, {_w(0)}; slot 3, {_w(2)}) is canceled too."
    )


def test_dj_channel_event_canceled() -> None:
    assert texts.dj_channel_event_canceled(_rave()) == (
        "**2026-11-28 Rave Night** on Saturday 28.11.2026 is canceled."
    )


def test_dj_line() -> None:
    assert texts.dj_line("DJ Alpha", _rave(), [0, 2]) == (
        f"- DJ Alpha, slot 1, {_w(0)}; slot 3, {_w(2)}"
    )


def test_log_event_canceled_text_without_and_with_djs() -> None:
    event = _rave()
    assert texts.log_event_canceled_text("Max", event, []) == (
        "Max canceled **2026-11-28 Rave Night**.\nNo DJ was booked."
    )
    line = texts.dj_line("DJ Alpha", event, [0])
    assert texts.log_event_canceled_text("Max", event, [line]) == (
        f"Max canceled **2026-11-28 Rave Night**.\nBooked DJs:\n- DJ Alpha, slot 1, {_w(0)}"
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
        f"- DJ Alpha, slot 1, {_w(0)}\n"
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
        "Some slots could not be emptied, see the log channel."
    )


def test_event_cancel_cleanup_failed_uses_slot_numbers() -> None:
    assert texts.event_cancel_cleanup_failed(_rave(), [0, 2]) == (
        "**2026-11-28 Rave Night** was canceled, but these slots could not be emptied: 1, 3."
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
        "EVENT_CANCEL_SAVE_FAILED", "EVENT_WAS_CANCELED",
    ):
        assert isinstance(getattr(texts, name), str) and getattr(texts, name)
    assert texts.LOG_EVENT_CANCELED_TITLE == "Event canceled"


def test_log_notion_missing_text_says_paused_only_when_paused() -> None:
    """M3: the log entry matches reality (paused only before the one-time import)."""
    from bot.ui import texts as t

    assert "paused" not in t.log_notion_missing_text("gone")
    assert "keep working" in t.log_notion_missing_text("gone")
    assert "paused" in t.log_notion_missing_text("gone", paused=True)


def test_dj_booking_line_has_no_bot_zone_date() -> None:
    assert texts.dj_booking_line(_rave(), 1) == f"- Rave Night, slot 2, {_w(1)}"


def test_rule_save_conflict_does_not_claim_a_new_schedule() -> None:
    text = texts.setup_rules_save_conflict([(1, "a", 2, "b")])
    assert "new schedule" not in text and "#1" in text and "#2" in text
