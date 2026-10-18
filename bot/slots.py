"""Slot count and slot occupancy — one source for every view, embed and outreach check.

Which slot index is booked by whom lives in Postgres (slot_locks), not in Notion: the Notion
DJs relation is an unordered list and only says how many DJs are linked, not which slot they
hold. The number of slots comes from the event's Notion "Slots" property when set, else from
/config set slots-per-event, else 6.

annotate() attaches that state to freshly fetched event dicts once; state() reads it back in
synchronous code such as embeds.
"""
from dataclasses import dataclass, field
from typing import Any

from bot.db.bookings import get_booked_slots
from bot.db.config import get_value

DEFAULT_SLOTS = 6
_KEY = "_ravebot_slots"


@dataclass(frozen=True)
class SlotState:
    total: int
    booked: dict[int, int] = field(default_factory=dict)  # slot_index -> discord_user_id

    @property
    def open_indices(self) -> list[int]:
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
    default_total = DEFAULT_SLOTS
    if pool is not None and guild_id is not None:
        raw = await get_value(pool, guild_id, "slots_per_event")
        if raw and raw.isdigit() and int(raw) > 0:
            default_total = int(raw)
    booked = await get_booked_slots(pool, [e["id"] for e in events if "id" in e]) if pool else {}
    for event in events:
        own = notion_slots(event)
        event[_KEY] = SlotState(
            total=own if own is not None else default_total,
            booked=booked.get(event.get("id", ""), {}),
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
