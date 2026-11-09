"""Dropdown option lists and value parsers for the setup wizard's value steps.

Pure functions: no Discord or database access. A value step shows `dropdown_options`
and, for "Other", accepts typed text checked by `parse_choice`.
"""
import datetime
import re
from typing import Any

from bot.notion.models import MAX_SLOTS
from bot.timezone import is_valid_timezone, local_today
from bot.ui import texts

LIMITS: dict[str, int] = {
    "slots_per_event": MAX_SLOTS,
    "slot_duration_minutes": 1440,
    "event_interval_days": 365,
    "event_lookahead_count": 52,
}

FIELD_LABELS: dict[str, str] = {
    "slots_per_event": "Slots per event",
    "default_start_time": "Start time (HH:MM)",
    "slot_duration_minutes": "Slot length (minutes)",
    "event_interval_days": "One event every (days)",
    "first_event_date": "First event date (YYYY-MM-DD)",
    "event_lookahead_count": "Future events kept ready",
    "timezone": "Time zone (e.g. Europe/Berlin)",
}

MAX_OPTIONS = 25
ASAP_LABEL = "As soon as the interval allows"
ASAP_VALUE = "asap"  # Discord rejects an empty option value

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ZONES = (
    "Europe/Berlin", "Europe/London", "Europe/Paris", "Europe/Vienna", "Europe/Zurich",
    "Europe/Amsterdam", "Europe/Madrid", "Europe/Rome", "Europe/Warsaw", "Europe/Helsinki",
    "Europe/Athens", "Europe/Istanbul", "UTC", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "America/Sao_Paulo", "Asia/Tokyo", "Asia/Dubai",
    "Australia/Sydney",
)
_NUMBER_CHOICES = {
    "slots_per_event": tuple(range(1, 26)),
    "slot_duration_minutes": (15, 20, 30, 45, 60, 90, 120),
    "event_interval_days": (7, 14, 21, 28),
    "event_lookahead_count": (1, 2, 3, 4, 6, 8, 12),
}
_START_TIMES = ("18:00", "19:00", "20:00", "21:00", "22:00", "23:00", "00:00", "01:00")


def parse_hhmm(text: str) -> str | None:
    """`H:MM` or `HH:MM` from 00:00 to 23:59 as zero-padded `HH:MM`, else None."""
    match = _HHMM_RE.match((text or "").strip())
    if match and int(match.group(1)) <= 23 and int(match.group(2)) <= 59:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def _date_label(day: datetime.date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day:02d} {_MONTHS[day.month - 1]}"


def _number_label(key: str, value: int) -> str:
    if key == "slot_duration_minutes":
        return f"{value} min"
    if key == "event_interval_days":
        return f"{value} days"
    return str(value)


def dropdown_options(
    key: str, current: Any, today: datetime.date | None = None
) -> list[tuple[str, str]]:
    """`(label, value)` pairs for the key's dropdown, at most 25.

    The stored `current` value is always included, replacing the last option when the
    list is full.
    """
    options: list[tuple[str, str]]
    if key in _NUMBER_CHOICES:
        options = [(_number_label(key, n), str(n)) for n in _NUMBER_CHOICES[key]]
        shown = (_number_label(key, int(current)), str(current)) if str(current).isdigit() else None
    elif key == "default_start_time":
        options = [(t, t) for t in _START_TIMES]
        shown = (str(current), str(current)) if current else None
    elif key == "timezone":
        options = [(z, z) for z in _ZONES]
        shown = (str(current), str(current)) if current else None
    else:  # first_event_date
        first = today or local_today()
        options = [(ASAP_LABEL, ASAP_VALUE)]
        for offset in range(MAX_OPTIONS - 1):
            day = first + datetime.timedelta(days=offset)
            options.append((_date_label(day), day.isoformat()))
        shown = None
        if current:
            try:
                shown = (_date_label(datetime.date.fromisoformat(str(current))), str(current))
            except ValueError:
                shown = None
    if shown is not None and shown[1] not in {value for _, value in options}:
        if len(options) >= MAX_OPTIONS:
            options.pop()
        options.append(shown)
    return options


def _number(key: str, raw: str) -> tuple[dict[str, Any], list[str]]:
    label, maximum = FIELD_LABELS[key], LIMITS[key]
    rule = f"enter a whole number from 1 to {maximum}."
    try:
        value = int((raw or "").strip())
    except ValueError:
        return {}, [texts.setup_invalid(label, rule)]
    if value < 1 or value > maximum:
        return {}, [texts.setup_invalid(label, rule)]
    return {key: value}, []


def parse_choice(
    key: str, raw: str, today: datetime.date | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Check a dropdown value or popup text for one key: (draft update, error texts)."""
    label = FIELD_LABELS[key]
    text = (raw or "").strip()
    if key in LIMITS:
        return _number(key, text)
    if key == "default_start_time":
        start = parse_hhmm(text)
        if start is None:
            return {}, [texts.setup_invalid(label, "enter a time from 00:00 to 23:59.")]
        return {key: start}, []
    if key == "timezone":
        if not text or not is_valid_timezone(text):
            return {}, [texts.setup_invalid(label, "enter a name like Europe/Berlin.")]
        return {key: text}, []
    if text in ("", ASAP_VALUE):
        return {key: ""}, []
    rule = "enter today or a later date like 2026-11-14."
    try:
        day = datetime.date.fromisoformat(text) if _DATE_RE.match(text) else None
    except ValueError:
        day = None
    if day is None or day < (today or local_today()):
        return {}, [texts.setup_invalid(label, rule)]
    return {key: day.isoformat()}, []
