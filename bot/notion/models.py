"""Typed models and parsers for raw Notion page JSON (API version 2026-03-11)."""
import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Events properties
PROP_EVENT_ID = "ID"
PROP_EVENT_DATE = "Date"
PROP_EVENT_STATUS = "Status"
PROP_EVENT_GENRE = "Genre"
PROP_EVENT_NOTES = "Notes"
PROP_EVENT_SLOTS = "Slots"
PROP_EVENT_START = "Start"
PROP_EVENT_SLOT_LENGTH = "Slot length"
# Slots properties
PROP_SLOT_EVENT = "Event"
PROP_SLOT_NUMBER = "Slot number"
PROP_SLOT_DJ = "DJ"
# DJs properties
PROP_DJ_NAME = "DJ name"
PROP_DJ_DISCORD_NAME = "Discord name"
PROP_DJ_DISCORD_ID = "Discord ID"

_START_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# Upper bound for an event's slot count (Notion's Slots number is free text).
MAX_SLOTS = 100


@dataclass(frozen=True)
class SlotInfo:
    page_id: str
    number: int
    dj_page_id: str | None
    dj_name: str | None
    discord_id: int | None
    extra_dj_page_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventInfo:
    page_id: str
    event_id: str
    date: datetime.date
    status: str | None
    genre: str | None
    notes: str | None
    slot_count: int
    start: datetime.time
    slot_minutes: int
    slots: tuple[SlotInfo, ...]

    def slot_start(self, slot_index: int) -> datetime.datetime:
        """UTC start of the zero-based slot_index."""
        base = datetime.datetime.combine(self.date, self.start, tzinfo=datetime.UTC)
        return base + datetime.timedelta(minutes=slot_index * self.slot_minutes)


@dataclass(frozen=True)
class DjInfo:
    page_id: str
    name: str
    discord_id: int | None
    discord_name: str | None


def _prop(page: dict, name: str) -> dict[str, Any]:
    return (page.get("properties") or {}).get(name) or {}


def _text(page: dict, name: str, key: str) -> str | None:
    parts = _prop(page, name).get(key) or []
    text = "".join(p.get("plain_text", "") for p in parts).strip()
    return text or None


def _title(page: dict, name: str) -> str | None:
    return _text(page, name, "title")


def _rich(page: dict, name: str) -> str | None:
    return _text(page, name, "rich_text")


def _number(page: dict, name: str) -> float | None:
    return _prop(page, name).get("number")


def _select(page: dict, name: str) -> str | None:
    sel = _prop(page, name).get("select")
    return sel.get("name") if sel else None


def _date(page: dict, name: str) -> datetime.date | None:
    d = _prop(page, name).get("date")
    start = d.get("start") if d else None
    if not start:
        return None
    try:
        return datetime.date.fromisoformat(start[:10])
    except ValueError:
        return None


def _relation_ids(page: dict, name: str) -> list[str]:
    return [r["id"] for r in (_prop(page, name).get("relation") or []) if r.get("id")]


def _parse_time(value: str | None) -> datetime.time | None:
    if not value:
        return None
    m = _START_RE.match(value)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return datetime.time(h, mi)


def _discord_id(page: dict) -> int | None:
    raw = _rich(page, PROP_DJ_DISCORD_ID)
    if raw and raw.isascii() and raw.isdigit():
        return int(raw)
    return None


def parse_event(page: dict) -> EventInfo | None:
    page_id = page.get("id", "")

    def reject(reason: str) -> None:
        logger.warning("Skipping event page %s: %s", page_id, reason)

    event_id = _title(page, PROP_EVENT_ID)
    if not event_id:
        reject(f"missing or empty {PROP_EVENT_ID}")
        return None
    date = _date(page, PROP_EVENT_DATE)
    if date is None:
        reject(f"missing {PROP_EVENT_DATE}")
        return None
    start = _parse_time(_rich(page, PROP_EVENT_START))
    if start is None:
        reject(f"{PROP_EVENT_START} is not a valid HH:MM time")
        return None
    slots = _number(page, PROP_EVENT_SLOTS)
    if slots is None or slots <= 0:
        reject(f"{PROP_EVENT_SLOTS} is empty or not positive")
        return None
    if slots > MAX_SLOTS:
        reject(f"{PROP_EVENT_SLOTS} is {slots:g}, more than {MAX_SLOTS}")
        return None
    length = _number(page, PROP_EVENT_SLOT_LENGTH)
    if length is None or length <= 0:
        reject(f"{PROP_EVENT_SLOT_LENGTH} is empty or not positive")
        return None
    return EventInfo(
        page_id=page_id,
        event_id=event_id,
        date=date,
        status=_select(page, PROP_EVENT_STATUS),
        genre=_rich(page, PROP_EVENT_GENRE),
        notes=_rich(page, PROP_EVENT_NOTES),
        slot_count=int(slots),
        start=start,
        slot_minutes=int(length),
        slots=(),
    )


def parse_dj(page: dict) -> DjInfo:
    return DjInfo(
        page_id=page.get("id", ""),
        name=_title(page, PROP_DJ_NAME) or "",
        discord_id=_discord_id(page),
        discord_name=_rich(page, PROP_DJ_DISCORD_NAME),
    )


def parse_slot(page: dict, djs: dict[str, DjInfo]) -> SlotInfo:
    # Notion ignores the relation limit of 1, so a hand edit can add more DJs.
    ids = _relation_ids(page, PROP_SLOT_DJ)
    dj_id = ids[0] if ids else None
    dj = djs.get(dj_id) if dj_id else None
    number = _number(page, PROP_SLOT_NUMBER)
    return SlotInfo(
        page_id=page.get("id", ""),
        number=int(number) if number is not None else 0,
        dj_page_id=dj_id,
        dj_name=dj.name if dj else None,
        discord_id=dj.discord_id if dj else None,
        extra_dj_page_ids=tuple(ids[1:]),
    )
