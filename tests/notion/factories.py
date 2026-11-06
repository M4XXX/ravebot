"""Builders for typed EventInfo / SlotInfo test fixtures."""
import datetime

from bot.notion.models import EventInfo, SlotInfo


def make_slot(
    number: int = 1,
    dj_page_id: str | None = None,
    dj_name: str | None = None,
    discord_id: int | None = None,
    page_id: str | None = None,
    extra_dj_page_ids: tuple[str, ...] = (),
) -> SlotInfo:
    return SlotInfo(
        page_id=page_id or f"slot-page-{number}",
        number=number,
        dj_page_id=dj_page_id,
        dj_name=dj_name,
        discord_id=discord_id,
        extra_dj_page_ids=extra_dj_page_ids,
    )


def make_event(
    event_id: str = "Friday Night Rave",
    page_id: str = "page-uuid",
    date: datetime.date = datetime.date(2026, 6, 13),
    status: str | None = "Planned",
    genre: str | None = None,
    notes: str | None = None,
    slot_count: int = 6,
    start: datetime.time = datetime.time(20, 0),
    slot_minutes: int = 60,
    slots: tuple[SlotInfo, ...] | list[SlotInfo] = (),
) -> EventInfo:
    return EventInfo(
        page_id=page_id,
        event_id=event_id,
        date=date,
        status=status,
        genre=genre,
        notes=notes,
        slot_count=slot_count,
        start=start,
        slot_minutes=slot_minutes,
        slots=tuple(slots),
    )
