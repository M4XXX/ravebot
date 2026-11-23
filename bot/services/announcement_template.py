"""Announcement templates: placeholder filling and the editor check. Pure, no database."""
import datetime
import re
from dataclasses import dataclass

from bot.notion.models import EventInfo, SlotInfo
from bot.timezone import local_today
from bot.ui.texts import escape_text, slot_time, ts

GENERAL = "general"
START = "start"
MAX_LENGTH = 2000

_COMMON = (
    "title",
    "date",
    "countdown",
    "description",
    "notes",
    "location",
    "genre",
    "lineup",
    "slots",
    "booked",
)
_LIVE = ("now_playing", "next_up", "status")
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    GENERAL: _COMMON + _LIVE,
    START: _COMMON + _LIVE,
}

DEFAULT_TEMPLATES: dict[str, str] = {
    GENERAL: (
        "**{title}**\n"
        "{date} ({countdown})\n"
        "{description}\n"
        "Location: {location}\n"
        "Genre: {genre}\n"
        "\n"
        "Lineup ({booked} of {slots} slots booked):\n"
        "{lineup}"
    ),
    START: (
        "**{title} is live!**\n"
        "Now playing: {now_playing}\n"
        "Up next: {next_up}\n"
        "\n"
        "{lineup}"
    ),
}

_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
_BAD_ROLE = re.compile(r"<@&(?!\d+>)")
_MAX_NAME = 100
_OPEN_LINE = "\U0001f7e2 *open slot*"


@dataclass(frozen=True)
class TemplateCheck:
    ok: bool
    problems: list[str]
    warnings: list[str]
    length: int
    preview: str


def _booked(slot: SlotInfo | None) -> bool:
    return slot is not None and slot.dj_page_id is not None


def _dj_name(slot: SlotInfo) -> str:
    name = slot.dj_name or "Guest DJ"
    suffix = ""
    if len(name) > _MAX_NAME:
        name, suffix = name[:_MAX_NAME], "…"
    return escape_text(name) + suffix


def event_end(event: EventInfo) -> datetime.datetime:
    """Last slot start plus the slot length (the start itself when there are no slots)."""
    if event.slot_count <= 0:
        return event.slot_start(0)
    return event.slot_start(event.slot_count - 1) + datetime.timedelta(minutes=event.slot_minutes)


def current_slot_index(event: EventInfo, now: datetime.datetime) -> int | None:
    """Zero-based index of the slot running at `now`, or None before, after or between."""
    for index in range(event.slot_count):
        start = event.slot_start(index)
        if start <= now < start + datetime.timedelta(minutes=event.slot_minutes):
            return index
    return None


def _slot_name(event: EventInfo, index: int) -> str:
    slot = event.resolved_slots().get(index + 1)
    return _dj_name(slot) if slot is not None and _booked(slot) else ""


def _values(event: EventInfo, now: datetime.datetime) -> dict[str, str]:
    by_number = event.resolved_slots()
    lines = []
    booked = 0
    for index in range(event.slot_count):
        slot = by_number.get(index + 1)
        stamp = slot_time(event, index)
        if slot is not None and _booked(slot):
            booked += 1
            lines.append(f"{stamp} {_dj_name(slot)}")
        else:
            lines.append(f"{stamp} {_OPEN_LINE}")
    current = current_slot_index(event, now)
    if now >= event_end(event):
        status, now_playing, next_up = "The event has ended.", "", ""
    elif current is None:
        status, now_playing = "Starting soon.", ""
        next_up = _slot_name(event, 0)
    else:
        status = "Live now."
        now_playing = _slot_name(event, current)
        next_up = _slot_name(event, current + 1) if current + 1 < event.slot_count else ""
    notes = escape_text(event.notes or "")
    return {
        "title": escape_text(event.event_id),
        "date": ts(event.slot_start(0), "F"),
        "countdown": ts(event.slot_start(0), "R"),
        "description": notes,
        "notes": notes,
        "location": escape_text(event.location or ""),
        "genre": escape_text(event.genre or ""),
        "lineup": "\n".join(lines),
        "slots": str(event.slot_count),
        "booked": str(booked),
        "now_playing": now_playing,
        "next_up": next_up,
        "status": status,
    }


def render(template: str, event: EventInfo, kind: str, now: datetime.datetime) -> str:
    """Fill the template. Unknown placeholders stay as typed; `{{` and `}}` are literal braces.

    A line whose only non-label content is placeholders that all render empty is dropped.
    """
    values = _values(event, now)
    allowed = PLACEHOLDERS.get(kind, PLACEHOLDERS[GENERAL])
    out: list[str] = []
    for line in template.split("\n"):
        known = 0
        filled = 0

        def fill(match: re.Match[str]) -> str:
            nonlocal known, filled
            token = match.group(0)
            if token == "{{":
                return "{"
            if token == "}}":
                return "}"
            name = match.group(1)
            if name not in allowed:
                filled += 1  # an unknown placeholder stays visible, so the line stays
                return token
            known += 1
            value = values[name]
            if value:
                filled += 1
            return value

        text = _TOKEN.sub(fill, line)
        if known and not filled:
            continue
        out.append(text)
    return "\n".join(out)


def sample_event() -> EventInfo:
    """A plausible event: 4 slots, two booked (one with a long DJ name), two open."""
    slots = (
        SlotInfo("s1", 1, "dj1", "DJ Nova", None),
        SlotInfo("s2", 2, "dj2", "The Extraordinarily Long Named DJ " * 3, None),
        SlotInfo("s3", 3, None, None, None),
        SlotInfo("s4", 4, None, None, None),
    )
    return EventInfo(
        page_id="sample",
        event_id="Friday Night Rave",
        date=local_today() + datetime.timedelta(days=7),
        status="Planned",
        genre="Techno",
        notes="Doors open half an hour before the first set.",
        slot_count=4,
        start=datetime.time(20, 0),
        slot_minutes=60,
        slots=slots,
        location="Main Hall",
    )


def check_template(template: str, kind: str, event: EventInfo | None) -> TemplateCheck:
    """Validate a template and fill it for `event` (or the sample event)."""
    allowed = PLACEHOLDERS.get(kind, PLACEHOLDERS[GENERAL])
    problems: list[str] = []
    warnings: list[str] = []
    unknown: list[str] = []
    for match in _TOKEN.finditer(template):
        name = match.group(1)
        if name is not None and name not in allowed and name not in unknown:
            unknown.append(name)
    if unknown:
        problems.append("Unknown placeholders: " + ", ".join("{" + n + "}" for n in unknown))
    if re.search(r"[{}]", _TOKEN.sub("", template)):
        problems.append(
            "Unbalanced braces: every { needs a matching }. Use {{ and }} for literal braces."
        )
    if _BAD_ROLE.search(template):
        problems.append("Malformed role mention: use <@&digits>, for example <@&123456789>.")
    for word in ("@everyone", "@here"):
        if word in template:
            warnings.append(f"{word} will not ping.")
    target = event or sample_event()
    preview = render(template, target, kind, target.slot_start(0) + datetime.timedelta(minutes=1))
    length = len(preview)
    if length > MAX_LENGTH:
        problems.append(
            f"Too long: {length} characters filled, the limit is {MAX_LENGTH} "
            f"({length - MAX_LENGTH} over)."
        )
    return TemplateCheck(not problems, problems, warnings, length, preview)
