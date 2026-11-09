"""Notion client for the Events, Slots and DJs data sources (new schema)."""
import dataclasses
import datetime
import logging
import urllib.parse
from typing import Any

from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.models import (
    MAX_SLOTS,
    PROP_DJ_DISCORD_ID,
    PROP_DJ_DISCORD_NAME,
    PROP_DJ_NAME,
    PROP_EVENT_DATE,
    PROP_EVENT_GENRE,
    PROP_EVENT_ID,
    PROP_EVENT_SLOT_LENGTH,
    PROP_EVENT_SLOTS,
    PROP_EVENT_START,
    PROP_EVENT_STATUS,
    PROP_SLOT_DJ,
    PROP_SLOT_EVENT,
    PROP_SLOT_NUMBER,
    DjInfo,
    EventInfo,
    SlotInfo,
    _relation_ids,
    _title,
    parse_dj,
    parse_event,
    parse_slot,
)
from bot.notion.schema import NotionIds
from bot.timezone import local_today

__all__ = [
    "DuplicateEventError",
    "NotionClient",
    "NotionWriteError",
    "SlotRowMissingError",
]

logger = logging.getLogger(__name__)

INITIAL_STATUS = "Planning not started"
PROP_SLOT_NAME = "Name"
PROP_DJ_VRCHAT_NAME = "VRChat name"
PROP_DJ_LOGO = "Logo"
PROP_DJ_LINK = "Link"
PROP_DJ_ALT_LINK = "Alternative link"


class DuplicateEventError(Exception):
    """An event with this ID already exists."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"Event ID already exists: {event_id}")
        self.event_id = event_id


class DjLogoUploadError(Exception):
    """upsert_dj wrote the DJ row but not its logo image.

    ``page_id`` is the written row; the caller keeps it (bookings work) and marks
    the logo unsynced so it is sent again later.
    """

    def __init__(self, page_id: str) -> None:
        super().__init__(f"DJ row {page_id} was written without its logo")
        self.page_id = page_id


class SlotRowMissingError(Exception):
    """The event has no Slots row with the requested slot number."""

    def __init__(self, event_id: str, number: int) -> None:
        super().__init__(f"Event {event_id!r} has no slot row number {number}")
        self.event_id = event_id
        self.number = number


def _is_trashed(page: dict[str, Any]) -> bool:
    return bool(page.get("in_trash") or page.get("is_archived"))


def _text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


def _is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parts = urllib.parse.urlsplit(value.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _url_prop(value: str | None) -> dict[str, Any]:
    return {"url": value.strip() if value and _is_http_url(value) else None}


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

        today = local_today()
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

        Raises ValueError for an empty ID, slot_count/slot_minutes below 1 or
        slot_count above MAX_SLOTS (before any request), DuplicateEventError if
        the ID exists.
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
        if slot_count > MAX_SLOTS:
            raise ValueError(f"slot_count must be at most {MAX_SLOTS}")
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
        event = EventInfo(
            page_id=page["id"],
            event_id=event_id,
            date=date,
            status=INITIAL_STATUS,
            genre=genre or None,
            notes=None,
            slot_count=slot_count,
            start=start,
            slot_minutes=slot_minutes,
            slots=(),
        )
        slots = await self.create_slot_rows(event, list(range(1, slot_count + 1)))
        return dataclasses.replace(event, slots=tuple(slots))

    async def restore_event_shape(
        self, event: EventInfo, start: datetime.time, slot_minutes: int
    ) -> None:
        """PATCH the Events row's ``Start`` and ``Slot length`` back to the given values.

        Used by the sync service when a hand edit changes them while DJs are booked.
        Raises NotionWriteError after failed retries.
        """
        await self._api.request(
            "PATCH",
            f"/pages/{event.page_id}",
            {
                "properties": {
                    PROP_EVENT_START: {"rich_text": _text(start.strftime("%H:%M"))},
                    PROP_EVENT_SLOT_LENGTH: {"number": slot_minutes},
                }
            },
            f"restore Start/Slot length of event {event.event_id}",
        )

    async def create_slot_rows(self, event: EventInfo, numbers: list[int]) -> list[SlotInfo]:
        """Create one empty Slots row per given number, linked to the event.

        Rows are created in the given order; a failure raises NotionWriteError
        and the rows created before it stay.
        """
        rows: list[SlotInfo] = []
        for number in numbers:
            slot_page = await self._api.request(
                "POST",
                "/pages",
                {
                    "parent": self._parent(self._ids.slots_ds),
                    "properties": {
                        PROP_SLOT_NAME: {"title": _text(f"{event.event_id} #{number}")},
                        PROP_SLOT_EVENT: {"relation": [{"id": event.page_id}]},
                        PROP_SLOT_NUMBER: {"number": number},
                    },
                },
                f"create slot {number} of event {event.event_id}",
            )
            rows.append(SlotInfo(slot_page["id"], number, None, None, None))
        return rows

    async def trash_slot_rows(self, rows: list[SlotInfo]) -> None:
        """Move the given Slots rows to the Notion trash (one PATCH per row)."""
        for row in rows:
            await self._api.request(
                "PATCH",
                f"/pages/{row.page_id}",
                {"in_trash": True},
                f"trash slot row {row.number} ({row.page_id})",
            )

    async def upsert_dj(
        self,
        discord_id: int,
        dj_name: str,
        discord_name: str,
        *,
        vrchat_name: str | None,
        logo_url: str | None,
        link: str | None,
        alt_link: str | None,
        logo_image: bytes | None = None,
        upload_logo: bool = True,
    ) -> str:
        """Create or update the DJs row matched by ``Discord ID``; return its page id.

        Never creates a duplicate. ``Logo``/``Link``/``Alternative link`` are only
        written for http(s) URLs, otherwise left empty. A ``logo_image`` is uploaded
        to Notion (file upload) and takes precedence over ``logo_url``. Rollups are
        never written.

        With ``upload_logo`` False an existing row keeps its ``Logo`` untouched (it
        is left out of the update); a new row always gets the logo. If the image
        cannot be uploaded or the row write rejects it, the row is written without
        ``Logo`` and DjLogoUploadError(page_id) is raised afterwards.
        """
        digits = str(discord_id)
        pages = await self._api.query_all(
            self._ids.djs_ds,
            {"filter": {"property": PROP_DJ_DISCORD_ID, "rich_text": {"equals": digits}}},
        )
        # Compare again: do not rely on the server applying the filter.
        existing = next(
            (
                p
                for p in pages
                if not _is_trashed(p) and parse_dj(p).discord_id == discord_id
            ),
            None,
        )
        props: dict[str, Any] = {
            PROP_DJ_NAME: {"title": _text(dj_name)},
            PROP_DJ_DISCORD_NAME: {"rich_text": _text(discord_name) if discord_name else []},
            PROP_DJ_DISCORD_ID: {"rich_text": _text(digits)},
            PROP_DJ_VRCHAT_NAME: {"rich_text": _text(vrchat_name) if vrchat_name else []},
            PROP_DJ_LINK: _url_prop(link),
            PROP_DJ_ALT_LINK: _url_prop(alt_link),
        }
        logo_failed = False
        if upload_logo or existing is None:
            if logo_image is not None:
                try:
                    upload_id = await self._api.upload_file(
                        "logo.png", "image/png", logo_image, f"logo of DJ {discord_id}"
                    )
                except NotionWriteError:
                    logger.warning("Logo upload of DJ %s failed", discord_id, exc_info=True)
                    logo_failed = True
                else:
                    props[PROP_DJ_LOGO] = {"files": [
                        {"name": "logo.png", "type": "file_upload",
                         "file_upload": {"id": upload_id}}
                    ]}
            elif logo_url and _is_http_url(logo_url):
                props[PROP_DJ_LOGO] = {"files": [
                    {"name": "logo", "type": "external", "external": {"url": logo_url.strip()}}
                ]}
            else:
                props[PROP_DJ_LOGO] = {"files": []}
        try:
            page_id = await self._write_dj(existing, props, discord_id)
        except NotionWriteError:
            if logo_image is None or PROP_DJ_LOGO not in props:
                raise
            # The row write may have rejected the uploaded file: write it without.
            logger.warning("Writing DJ %s with logo failed; retrying without", discord_id)
            del props[PROP_DJ_LOGO]
            page_id = await self._write_dj(existing, props, discord_id)
            logo_failed = True
        if logo_failed:
            raise DjLogoUploadError(page_id)
        return page_id

    async def _write_dj(
        self, existing: dict[str, Any] | None, props: dict[str, Any], discord_id: int
    ) -> str:
        if existing is not None:
            await self._api.request(
                "PATCH",
                f"/pages/{existing['id']}",
                {"properties": props},
                f"update DJ {discord_id}",
            )
            return existing["id"]
        page = await self._api.request(
            "POST",
            "/pages",
            {"parent": self._parent(self._ids.djs_ds), "properties": props},
            f"create DJ {discord_id}",
        )
        return page["id"]

    async def trash_dj_page(self, page_id: str) -> None:
        """Move a DJ row of the Notion DJs database to the trash (a 404 is fine)."""
        try:
            await self._api.request(
                "PATCH", f"/pages/{page_id}", {"in_trash": True},
                f"trash DJ page {page_id}", tolerate=(404,),
            )
        except NotionWriteError as exc:
            if exc.status_code != 404:
                raise

    async def dj_page_alive(self, page_id: str) -> bool:
        """True if the DJ page exists and is not in the trash.

        GET /pages/{id}: 404 or ``in_trash``/``is_archived`` give False (a 404 is
        not posted to the admin log). Other failures raise NotionWriteError.
        """
        try:
            page = await self._api.request(
                "GET", f"/pages/{page_id}", None, f"check DJ page {page_id}",
                tolerate=(404,),
            )
        except NotionWriteError as exc:
            if exc.status_code == 404:
                return False
            raise
        return not _is_trashed(page)

    async def refresh_discord_name(self, dj_page_id: str, discord_name: str) -> None:
        """PATCH only the ``Discord name`` property of a DJ row."""
        await self._api.request(
            "PATCH",
            f"/pages/{dj_page_id}",
            {"properties": {PROP_DJ_DISCORD_NAME: {"rich_text": _text(discord_name)}}},
            f"refresh Discord name of DJ page {dj_page_id}",
        )

    @staticmethod
    def _slot_row(event: EventInfo, number: int) -> SlotInfo:
        """The event's Slots row with this number; duplicates resolve to the lowest
        page id (the same rule as the sync planner). Raises SlotRowMissingError."""
        rows = [s for s in event.slots if s.number == number]
        if not rows:
            raise SlotRowMissingError(event.event_id, number)
        return min(rows, key=lambda s: s.page_id)

    async def _fresh_slot_dj_ids(
        self, event: EventInfo, slot_index: int
    ) -> tuple[str, list[str]]:
        """Return (slot row page id, DJ ids now in its relation), read just now."""
        number = slot_index + 1
        row = self._slot_row(event, number)
        page = await self._api.request(
            "GET", f"/pages/{row.page_id}", None, f"read slot {number} of {event.event_id}"
        )
        if _is_trashed(page):
            raise SlotRowMissingError(event.event_id, number)
        return row.page_id, _relation_ids(page, PROP_SLOT_DJ)

    async def _write_slot_djs(
        self, page_id: str, dj_page_ids: list[str], what: str
    ) -> None:
        await self._api.request(
            "PATCH",
            f"/pages/{page_id}",
            {"properties": {PROP_SLOT_DJ: {"relation": [{"id": i} for i in dj_page_ids]}}},
            what,
        )

    async def assign_slot(self, event: EventInfo, slot_index: int, dj_page_id: str) -> bool:
        """Put the DJ on the slot row; False (nothing written) if it already has any DJ.

        Re-reads the row right before writing, so a stale ``event`` cannot
        overwrite a booking made in the meantime. The remaining gap between the
        GET and the PATCH is closed for DJ bookings by the Postgres FOR UPDATE
        slot lock taken before this call; callers that do not take that lock
        (admin bookings, Task 11) must take it or accept the race. Raises SlotRowMissingError if
        the event has no row for the slot, NotionWriteError after failed retries.
        """
        page_id, current = await self._fresh_slot_dj_ids(event, slot_index)
        if current:
            return False
        await self._write_slot_djs(
            page_id, [dj_page_id], f"book slot {slot_index + 1} of {event.event_id}"
        )
        return True

    async def clear_slot(self, event: EventInfo, slot_index: int, dj_page_id: str) -> bool:
        """Remove only this DJ from the slot row.

        True if the DJ was removed or the row was already empty; False (nothing
        written) if the row holds only other DJs. Re-reads the row first.
        """
        page_id, current = await self._fresh_slot_dj_ids(event, slot_index)
        if not current:
            return True
        if dj_page_id not in current:
            return False
        await self._write_slot_djs(
            page_id,
            [i for i in current if i != dj_page_id],
            f"cancel slot {slot_index + 1} of {event.event_id}",
        )
        return True

    async def force_assign_slot(
        self, event: EventInfo, slot_index: int, dj_page_id: str
    ) -> None:
        """Set the slot row's DJ relation to exactly this DJ (admin override only).

        Unconditionally replaces any holder and any extra DJs; no re-read, no
        conflict check. Raises SlotRowMissingError if the event has no row for
        the slot, NotionWriteError after failed retries.
        """
        number = slot_index + 1
        row = self._slot_row(event, number)
        await self._write_slot_djs(
            row.page_id, [dj_page_id], f"admin book slot {number} of {event.event_id}"
        )

    async def keep_first_dj(self, event: EventInfo, slot_index: int) -> None:
        """Reduce the slot row's DJ relation to its first entry (sync of hand edits).

        Re-reads the row first; writes nothing if it holds one DJ or none.
        Raises SlotRowMissingError if the event has no row for the slot,
        NotionWriteError after failed retries.
        """
        page_id, current = await self._fresh_slot_dj_ids(event, slot_index)
        if len(current) <= 1:
            return
        await self._write_slot_djs(
            page_id,
            current[:1],
            f"remove extra DJs from slot {slot_index + 1} of {event.event_id}",
        )

    @staticmethod
    def _parent(data_source_id: str) -> dict[str, str]:
        return {"type": "data_source_id", "data_source_id": data_source_id}
