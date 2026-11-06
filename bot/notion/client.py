"""Notion client for the Events, Slots and DJs data sources (new schema)."""
import dataclasses
import datetime
import logging
from typing import Any

from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.models import (
    PROP_EVENT_DATE,
    PROP_EVENT_GENRE,
    PROP_EVENT_ID,
    PROP_EVENT_SLOT_LENGTH,
    PROP_EVENT_SLOTS,
    PROP_EVENT_START,
    PROP_EVENT_STATUS,
    PROP_SLOT_EVENT,
    PROP_SLOT_NUMBER,
    DjInfo,
    EventInfo,
    SlotInfo,
    _title,
    parse_dj,
    parse_event,
    parse_slot,
)
from bot.notion.schema import NotionIds

__all__ = ["DuplicateEventError", "NotionClient", "NotionWriteError"]

logger = logging.getLogger(__name__)

INITIAL_STATUS = "Planning not started"
PROP_SLOT_NAME = "Name"


class DuplicateEventError(Exception):
    """An event with this ID already exists."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"Event ID already exists: {event_id}")
        self.event_id = event_id


def _is_trashed(page: dict[str, Any]) -> bool:
    return bool(page.get("in_trash") or page.get("is_archived"))


def _text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


class NotionClient:
    def __init__(self, api: NotionApi, ids: NotionIds) -> None:
        self._api = api
        self._ids = ids

    async def _rows(self, data_source_id: str) -> list[dict[str, Any]]:
        pages = await self._api.query_all(data_source_id)
        return [p for p in pages if not _is_trashed(p)]

    async def event_id_exists(self, event_id: str) -> bool:
        pages = await self._api.query_all(
            self._ids.events_ds,
            {"filter": {"property": PROP_EVENT_ID, "title": {"equals": event_id}}},
        )
        # Compare titles again: do not rely on the server applying the filter.
        return any(
            not _is_trashed(p) and _title(p, PROP_EVENT_ID) == event_id for p in pages
        )

    async def list_events(self, *, include_past: bool = False) -> list[EventInfo]:
        """All events with slots and DJs joined, sorted by (date, start).

        An event whose slot rows were only partly created (see create_event)
        shows fewer slots than slot_count until the sync task repairs it.
        """
        event_pages = await self._rows(self._ids.events_ds)
        slot_pages = await self._rows(self._ids.slots_ds)
        dj_pages = await self._rows(self._ids.djs_ds)

        djs: dict[str, DjInfo] = {}
        for page in dj_pages:
            dj = parse_dj(page)
            djs[dj.page_id] = dj

        events: dict[str, EventInfo] = {}
        for page in event_pages:
            event = parse_event(page)
            if event is not None:
                events[event.page_id] = event

        by_event: dict[str, list[SlotInfo]] = {}
        invalid_numbers = 0
        for page in slot_pages:
            rel = (page.get("properties") or {}).get(PROP_SLOT_EVENT) or {}
            event_ids = [r["id"] for r in rel.get("relation") or [] if r.get("id")]
            if not event_ids:
                continue
            slot = parse_slot(page, djs)
            if slot.number < 1:
                invalid_numbers += 1
                continue
            by_event.setdefault(event_ids[0], []).append(slot)
        if invalid_numbers:
            logger.warning(
                "Skipped %d slot row(s) without a valid slot number", invalid_numbers
            )

        today = datetime.datetime.now(datetime.UTC).date()
        result = []
        for page_id, event in events.items():
            if not include_past and event.date < today:
                continue
            slots = tuple(sorted(by_event.get(page_id, ()), key=lambda s: s.number))
            result.append(dataclasses.replace(event, slots=slots))
        result.sort(key=lambda e: (e.date, e.start))
        return result

    async def create_event(
        self,
        event_id: str,
        date: datetime.date,
        start: datetime.time,
        slot_count: int,
        slot_minutes: int,
        *,
        genre: str | None = None,
    ) -> EventInfo:
        """Create the Events row, then one Slots row per slot (no DJ).

        Raises ValueError for an empty ID or slot_count/slot_minutes below 1
        (before any request), DuplicateEventError if the ID exists.
        If a slot row fails after retries, NotionWriteError propagates and the
        already created event stays; list_events then shows it with missing
        slot rows. Calling create_event again with the same ID raises
        DuplicateEventError, so it is not retryable; the sync service's
        "create_slots" action (Task 12) repairs events with missing slot rows.
        """
        event_id = event_id.strip()
        if not event_id:
            raise ValueError("event_id must not be empty")
        if slot_count < 1 or slot_minutes < 1:
            raise ValueError("slot_count and slot_minutes must be at least 1")
        if await self.event_id_exists(event_id):
            raise DuplicateEventError(event_id)

        props: dict[str, Any] = {
            PROP_EVENT_ID: {"title": _text(event_id)},
            PROP_EVENT_DATE: {"date": {"start": date.isoformat()}},
            PROP_EVENT_STATUS: {"select": {"name": INITIAL_STATUS}},
            PROP_EVENT_SLOTS: {"number": slot_count},
            PROP_EVENT_START: {"rich_text": _text(start.strftime("%H:%M"))},
            PROP_EVENT_SLOT_LENGTH: {"number": slot_minutes},
        }
        if genre:
            props[PROP_EVENT_GENRE] = {"rich_text": _text(genre)}
        page = await self._api.request(
            "POST",
            "/pages",
            {"parent": self._parent(self._ids.events_ds), "properties": props},
            f"create event {event_id}",
        )
        event_page_id = page["id"]

        slots: list[SlotInfo] = []
        for number in range(1, slot_count + 1):
            slot_page = await self._api.request(
                "POST",
                "/pages",
                {
                    "parent": self._parent(self._ids.slots_ds),
                    "properties": {
                        PROP_SLOT_NAME: {"title": _text(f"{event_id} #{number}")},
                        PROP_SLOT_EVENT: {"relation": [{"id": event_page_id}]},
                        PROP_SLOT_NUMBER: {"number": number},
                    },
                },
                f"create slot {number} of event {event_id}",
            )
            slots.append(SlotInfo(slot_page["id"], number, None, None, None))
        return EventInfo(
            page_id=event_page_id,
            event_id=event_id,
            date=date,
            status=INITIAL_STATUS,
            genre=genre or None,
            notes=None,
            slot_count=slot_count,
            start=start,
            slot_minutes=slot_minutes,
            slots=tuple(slots),
        )

    @staticmethod
    def _parent(data_source_id: str) -> dict[str, str]:
        return {"type": "data_source_id", "data_source_id": data_source_id}
