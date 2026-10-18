"""Slot count and slot occupancy — one source for every view, embed and outreach check.

Which slot index is booked by whom lives in Postgres (slot_locks), not in Notion: the Notion
DJs relation is an unordered list and only says how many DJs are linked, not which slot they
hold. The number of slots comes from the event's Notion "Slots" property when set, else from
/config set slots-per-event, else 6.

annotate() attaches that state to freshly fetched event dicts once; state() reads it back in
synchronous code such as embeds.

With NOTION_LAYOUT=tds the event database is an organizers' plan with one text column per
hour ("21:00 - 22:00", "24:00 - 01:00", ...): a slot's column holds the DJ's name. A name
typed in by hand books the slot as well, and the event's status decides whether DJs may book.
"""
from dataclasses import dataclass, field
from typing import Any

from bot import schedule
from bot.db.bookings import get_booked_slots
from bot.db.config import get_value
from bot.notion import props

DEFAULT_SLOTS = 6
_KEY = "_ravebot_slots"

CLASSIC = "classic"
TDS = "tds"

_layout = CLASSIC
_bookable_statuses: frozenset[str] = frozenset()


def configure(layout: str, bookable_statuses: frozenset[str] = frozenset()) -> None:
    """Set the Notion layout and the statuses DJs may book (empty = no status filter)."""
    global _layout, _bookable_statuses
    _layout = layout
    _bookable_statuses = frozenset(bookable_statuses)


def layout() -> str:
    return _layout


def bookable_statuses() -> frozenset[str]:
    return _bookable_statuses


def _clock(minutes: int) -> str:
    """Wall-clock label of a minute of the day; midnight is written 24:00, as in the plan."""
    hour, minute = divmod(minutes % (24 * 60), 60)
    return f"{24 if hour == 0 and minute == 0 else hour:02d}:{minute:02d}"


def column_name(slot_index: int, sched: "schedule.Schedule | None" = None) -> str:
    """Notion column of a slot in the tds layout, e.g. '21:00 - 22:00' or '24:00 - 01:00'.

    Columns are named by the wall-clock hours of the configured start time and slot length,
    not by real time: on the night the clocks change the plan keeps its column names.
    """
    sched = sched or schedule.current()
    start = sched.start.hour * 60 + sched.start.minute + slot_index * sched.slot_minutes
    return f"{_clock(start)} - {_clock(start + sched.slot_minutes)}"


def cell_text(event: dict, slot_index: int) -> str:
    """What the event's column for this slot says ('' when empty or missing)."""
    found = props.find_property(event, column_name(slot_index))
    return props.plain_text(found[1]) if found else ""


async def total_for(pool: Any, guild_id: int | None) -> int:
    """Slots per event from /config set slots-per-event, else DEFAULT_SLOTS."""
    if pool is not None and guild_id is not None:
        raw = await get_value(pool, guild_id, "slots_per_event")
        if raw and raw.isdigit() and int(raw) > 0:
            return int(raw)
    return DEFAULT_SLOTS


async def required_properties(pool: Any, guild_id: int | None) -> list[str]:
    """Notion columns the bot needs, checked at startup."""
    if _layout != TDS:
        return ["Name", "Date", "Slots", "DJs", "Discord ID"]
    if pool is not None and guild_id is not None:
        await schedule.refresh(pool, guild_id)  # the slot columns follow start time + length
    total = await total_for(pool, guild_id)
    required = ["Date"] + [column_name(i) for i in range(total)]
    if _bookable_statuses:
        required.append("Status")
    return required


def is_bookable(event: dict) -> bool:
    """True if DJs may book this event: no status filter, or its status is in the list."""
    if not _bookable_statuses:
        return True
    return props.status_name(event) in _bookable_statuses


@dataclass(frozen=True)
class SlotState:
    total: int
    booked: dict[int, int] = field(default_factory=dict)  # slot_index -> discord_user_id
    # tds layout: what each filled slot column says (a hand entry is booked with user id 0)
    names: dict[int, str] = field(default_factory=dict)
    bookable: bool = True

    @property
    def open_indices(self) -> list[int]:
        if not self.bookable:
            return []
        return [i for i in range(self.total) if i not in self.booked]

    @property
    def booked_count(self) -> int:
        return len([i for i in self.booked if i < self.total])


def notion_slots(event: dict) -> int | None:
    """The event's own Notion "Slots" value; None when the property is empty or missing.

    An explicit 0 is kept: it means the event has no DJ slots.
    """
    try:
        raw = event["properties"]["Slots"]["number"]
        return max(0, int(raw)) if raw is not None else None
    except (KeyError, TypeError, ValueError):
        return None


async def annotate(pool: Any, guild_id: int | None, events: list[dict]) -> list[dict]:
    """Attach the real slot state (total + booked indices from slot_locks) to each event."""
    default_total = await total_for(pool, guild_id)
    booked = await get_booked_slots(pool, [e["id"] for e in events if "id" in e]) if pool else {}
    for event in events:
        locks = booked.get(event.get("id", ""), {})
        if _layout == TDS:
            names = {i: t for i in range(default_total) if (t := cell_text(event, i))}
            event[_KEY] = SlotState(
                total=default_total,
                booked={**{i: 0 for i in names}, **locks},
                names=names,
                bookable=is_bookable(event),
            )
            continue
        own = notion_slots(event)
        event[_KEY] = SlotState(
            total=own if own is not None else default_total,
            booked=locks,
        )
    return events


def state(event: dict) -> SlotState:
    """Slot state attached by annotate(); without it, the old Notion count as a fallback.

    The fallback only knows how many DJs are linked, not which slots they hold, so callers
    that show or offer individual slots must annotate first.
    """
    attached = event.get(_KEY)
    if isinstance(attached, SlotState):
        return attached
    own = notion_slots(event)
    total = own if own is not None else DEFAULT_SLOTS
    try:
        linked = len(event["properties"]["DJs"]["relation"])
    except (KeyError, TypeError):
        linked = 0
    return SlotState(total=total, booked={i: 0 for i in range(min(linked, total))})
