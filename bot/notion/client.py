"""Notion client for the Events, Slots and DJs data sources (new schema)."""
import dataclasses
import datetime
import json
import logging
from typing import Any, Literal

from bot.db.events import DuplicateEventError
from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.models import (
    BOTDATA_TOO_LARGE_NOTE,
    PROP_BOTDATA_KEY,
    PROP_BOTDATA_PAYLOAD,
    PROP_BOTDATA_TABLE,
    PROP_DJ_ALT_LINK,
    PROP_DJ_DISCORD_ID,
    PROP_DJ_DISCORD_NAME,
    PROP_DJ_LINK,
    PROP_DJ_LOGO,
    PROP_DJ_NAME,
    PROP_DJ_VRCHAT_NAME,
    PROP_EVENT_BOT_ID,
    PROP_EVENT_DATE,
    PROP_EVENT_GENRE,
    PROP_EVENT_ID,
    PROP_EVENT_LOCATION,
    PROP_EVENT_NOTES,
    PROP_EVENT_SLOT_LENGTH,
    PROP_EVENT_SLOTS,
    PROP_EVENT_START,
    PROP_EVENT_STATUS,
    PROP_SLOT_DJ,
    PROP_SLOT_EVENT,
    PROP_SLOT_NUMBER,
    is_http_url,
    is_trashed,
    url_prop,
)
from bot.notion.schema import NotionIds

__all__ = [
    "DuplicateEventError",
    "NotionClient",
    "NotionSnapshot",
    "NotionWriteError",
]

logger = logging.getLogger(__name__)

PROP_SLOT_NAME = "Name"


class DjLogoUploadError(Exception):
    """mirror_dj wrote the DJ row but not its logo image.

    ``page_id`` is the written row; the mirror worker records it and retries the
    logo through the outbox backoff (``logo_synced`` stays false).
    """

    def __init__(self, page_id: str) -> None:
        super().__init__(f"DJ row {page_id} was written without its logo")
        self.page_id = page_id


def _text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


_RICH_TEXT_LIMIT = 2000
_RICH_TEXT_MAX_CHUNKS = 100


@dataclasses.dataclass
class NotionSnapshot:
    events: list[dict[str, Any]]
    slots: list[dict[str, Any]]
    djs: list[dict[str, Any]]
    botdata: list[dict[str, Any]]


class NotionClient:
    def __init__(self, api: NotionApi, ids: NotionIds) -> None:
        self._api = api
        self._ids = ids

    async def _rows(self, data_source_id: str) -> list[dict[str, Any]]:
        pages = await self._api.query_all(data_source_id)
        return [p for p in pages if not is_trashed(p)]

    async def _logo_prop(
        self, discord_id: int, logo_url: str | None, logo_image: bytes | None
    ) -> tuple[dict[str, Any] | None, bool]:
        """The ``Logo`` property value and whether the image upload failed.

        None means "leave the property out" (failed upload).
        """
        if logo_image is not None:
            try:
                upload_id = await self._api.upload_file(
                    "logo.png", "image/png", logo_image, f"logo of DJ {discord_id}"
                )
            except NotionWriteError:
                logger.warning("Logo upload of DJ %s failed", discord_id, exc_info=True)
                return None, True
            return {"files": [
                {"name": "logo.png", "type": "file_upload",
                 "file_upload": {"id": upload_id}}
            ]}, False
        if logo_url and is_http_url(logo_url):
            return {"files": [
                {"name": "logo", "type": "external", "external": {"url": logo_url.strip()}}
            ]}, False
        return {"files": []}, False

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

    async def trash_page(self, page_id: str) -> None:
        """Move any page to the Notion trash (a 404 is fine)."""
        try:
            await self._api.request(
                "PATCH", f"/pages/{page_id}", {"in_trash": True},
                f"trash page {page_id}", tolerate=(404,),
            )
        except NotionWriteError as exc:
            if exc.status_code != 404:
                raise

    # ---- mirror writes: Postgres is the truth, write exactly the payload ----

    async def _page_alive(self, page_id: str | None) -> bool:
        if page_id is None:
            return False
        try:
            page = await self._api.request(
                "GET", f"/pages/{page_id}", None, f"check page {page_id}",
                tolerate=(404,),
            )
        except NotionWriteError as exc:
            if exc.status_code == 404:
                return False
            raise
        return not is_trashed(page)

    async def _mirror_page(
        self, ds_id: str, props: dict[str, Any], page_id: str | None, context: str
    ) -> str:
        """PATCH the page when it is alive, else create a new one; return its id."""
        if await self._page_alive(page_id):
            await self._api.request(
                "PATCH", f"/pages/{page_id}", {"properties": props}, f"update {context}"
            )
            return str(page_id)
        page = await self._api.request(
            "POST",
            "/pages",
            {"parent": self._parent(ds_id), "properties": props},
            f"create {context}",
        )
        return str(page["id"])

    async def mirror_event(self, uid: str, payload: dict[str, Any], page_id: str | None) -> str:
        """Write the event payload to its Events row (created when missing)."""
        props: dict[str, Any] = {
            PROP_EVENT_ID: {"title": _text(payload["event_id"])},
            PROP_EVENT_DATE: {"date": {"start": payload["date"]}},
            PROP_EVENT_STATUS: {
                "select": {"name": payload["status"]} if payload.get("status") else None
            },
            PROP_EVENT_SLOTS: {"number": payload["slot_count"]},
            PROP_EVENT_START: {"rich_text": _text(payload["start"])},
            PROP_EVENT_SLOT_LENGTH: {"number": payload["slot_minutes"]},
            PROP_EVENT_GENRE: {
                "rich_text": _text(payload["genre"]) if payload.get("genre") else []
            },
            PROP_EVENT_LOCATION: {
                "rich_text": _text(payload["location"]) if payload.get("location") else []
            },
            PROP_EVENT_NOTES: {
                "rich_text": _text(payload["notes"]) if payload.get("notes") else []
            },
            PROP_EVENT_BOT_ID: {"rich_text": _text(uid)},
        }
        return await self._mirror_page(
            self._ids.events_ds, props, page_id, f"event {payload['event_id']}"
        )

    async def mirror_slot(
        self,
        payload: dict[str, Any],
        event_page_id: str,
        dj_page_id: str | None,
        page_id: str | None,
        *,
        event_label: str | None = None,
    ) -> str:
        """Write the slot payload to its Slots row (created when missing).

        ``event_label`` (the event ID) only feeds the row's ``Name`` title.
        """
        number = payload["number"]
        name = f"{event_label} #{number}" if event_label else f"Slot {number}"
        props: dict[str, Any] = {
            PROP_SLOT_NAME: {"title": _text(name)},
            PROP_SLOT_EVENT: {"relation": [{"id": event_page_id}]},
            PROP_SLOT_NUMBER: {"number": number},
            PROP_SLOT_DJ: {"relation": [{"id": dj_page_id}] if dj_page_id else []},
        }
        return await self._mirror_page(
            self._ids.slots_ds, props, page_id, f"slot {number} of event {payload['event_uid']}"
        )

    async def mirror_dj(
        self,
        payload: dict[str, Any],
        page_id: str | None,
        *,
        logo_image: bytes | None,
        upload_logo: bool,
        logo_url: str | None = None,
        discord_name: str | None = None,
    ) -> str:
        """Write the DJ payload to its DJs row (created when missing).

        The logo is not part of the payload: pass ``logo_url``/``logo_image`` separately.
        ``discord_name`` (not compared either) is written to "Discord name" when given;
        None leaves the cell as it is.
        Logo rules: with ``upload_logo`` False an existing row keeps
        its ``Logo``; a new row always gets it. A failed logo upload still writes the
        row and then raises DjLogoUploadError(page_id).
        """
        discord_id = payload["discord_id"]
        props: dict[str, Any] = {
            PROP_DJ_NAME: {"title": _text(payload["dj_name"])},
            PROP_DJ_DISCORD_ID: {"rich_text": _text(str(discord_id))},
            PROP_DJ_VRCHAT_NAME: {
                "rich_text": _text(payload["vrchat_name"]) if payload.get("vrchat_name") else []
            },
            PROP_DJ_LINK: url_prop(payload.get("link")),
            PROP_DJ_ALT_LINK: url_prop(payload.get("alt_link")),
        }
        if discord_name is not None:
            props[PROP_DJ_DISCORD_NAME] = {"rich_text": _text(discord_name)}
        alive = await self._page_alive(page_id)
        existing = {"id": page_id} if alive else None
        logo_failed = False
        if upload_logo or existing is None:
            logo_prop, logo_failed = await self._logo_prop(
                discord_id, logo_url, logo_image
            )
            if logo_prop is not None:
                props[PROP_DJ_LOGO] = logo_prop
        try:
            new_id = await self._write_dj(existing, props, discord_id)
        except NotionWriteError:
            if logo_image is None or PROP_DJ_LOGO not in props:
                raise
            logger.warning("Writing DJ %s with logo failed; retrying without", discord_id)
            del props[PROP_DJ_LOGO]
            new_id = await self._write_dj(existing, props, discord_id)
            logo_failed = True
        if logo_failed:
            raise DjLogoUploadError(new_id)
        return new_id

    async def mirror_botdata(
        self, key: str, table: str, payload: dict[str, Any], page_id: str | None
    ) -> str:
        """Write one Bot data row (read-only backup) to its page (created when missing).

        The JSON payload is split into 2000-character rich text chunks. A payload beyond
        Notion's 100 chunks per property is left out and the ``Note`` says so.
        """
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        chunks = [text[i : i + _RICH_TEXT_LIMIT] for i in range(0, len(text), _RICH_TEXT_LIMIT)]
        note = ""
        if len(chunks) > _RICH_TEXT_MAX_CHUNKS:
            chunks, note = [], BOTDATA_TOO_LARGE_NOTE
        props: dict[str, Any] = {
            PROP_BOTDATA_KEY: {"title": _text(key)},
            PROP_BOTDATA_TABLE: {"select": {"name": table}},
            PROP_BOTDATA_PAYLOAD: {"rich_text": [_text(c)[0] for c in chunks]},
            "Updated": {
                "date": {"start": datetime.datetime.now(datetime.UTC).isoformat()}
            },
            "Note": {"rich_text": _text(note) if note else []},
        }
        return await self._mirror_page(
            self._ids.botdata_ds, props, page_id, f"bot data {key}"
        )

    async def has_rows(self, table: Literal["events", "slots", "djs"]) -> bool:
        """True when the Events, Slots or DJs database holds a live page (reads one page)."""
        ds_id = {
            "events": self._ids.events_ds,
            "slots": self._ids.slots_ds,
            "djs": self._ids.djs_ds,
        }[table]
        data = await self._api.request(
            "POST", f"/data_sources/{ds_id}/query", {"page_size": 1}, f"check {table} for rows"
        )
        return any(not is_trashed(p) for p in data.get("results") or [])

    async def read_all(self) -> "NotionSnapshot":
        """All non-trashed pages of the mirrored databases (raw JSON)."""
        return NotionSnapshot(
            events=await self._rows(self._ids.events_ds),
            slots=await self._rows(self._ids.slots_ds),
            djs=await self._rows(self._ids.djs_ds),
            botdata=await self._rows(self._ids.botdata_ds) if self._ids.botdata_ds else [],
        )

    @staticmethod
    def _parent(data_source_id: str) -> dict[str, str]:
        return {"type": "data_source_id", "data_source_id": data_source_id}
