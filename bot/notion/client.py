"""Notion REST API client — exponential backoff, schema validation.

Story 1.5 scope: NotionWriteError, NotionClient with _write_with_retry() and validate_schema().
Story 3.1 scope: poll_events() — fetch future events for change detection.
Story 7.1 scope: create_event() — auto-create and manual event creation in Notion.
Full read_slots(), write_booking(), delete_booking() are Story 3.x scope.

[Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
[Source: architecture.md#Error Routing: Centralized AdminLogService]
"""
import asyncio
import datetime
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Retry delays in seconds: attempt 1 → wait 1s, attempt 2 → wait 2s, attempt 3 → fail
_RETRY_DELAYS = (1.0, 2.0)

# Body keys Notion reads to move a page, block or database to the trash.
_TRASH_KEYS = ("archived", "in_trash", "is_archived")


class NotionWriteError(Exception):
    """Raised when a Notion write fails after all retry attempts."""


class NotionDeletionBlockedError(Exception):
    """Raised when a request would delete or trash Notion content.

    The bot never deletes anything in Notion — removing content is left to a human.
    """


def _sets_trash_flag(value: Any) -> bool:
    """True if any nested dict sets one of the trash keys to True."""
    if isinstance(value, dict):
        return any(
            (key in _TRASH_KEYS and item is True) or _sets_trash_flag(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_sets_trash_flag(item) for item in value)
    return False


async def _refuse_deletion(request: httpx.Request) -> None:
    """httpx request hook: block DELETE and archive/trash writes before they are sent.

    Registered on the client itself, so it also covers code paths added later.
    """
    if request.method == "DELETE":
        logger.error("Blocked Notion DELETE %s — deletion is human-only", request.url.path)
        raise NotionDeletionBlockedError(f"DELETE {request.url.path} blocked")
    body = await request.aread()
    if not body:
        return
    try:
        payload = json.loads(body)
    except ValueError:
        return
    if _sets_trash_flag(payload):
        logger.error(
            "Blocked Notion %s %s that would trash content — deletion is human-only",
            request.method,
            request.url.path,
        )
        raise NotionDeletionBlockedError(
            f"{request.method} {request.url.path} would trash content"
        )


class NotionClient:
    """Async Notion REST API client with retry/backoff and schema validation.

    All requests use HTTPS via httpx (default). Authorization header is set
    at construction time — never logged.

    [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
    """

    def __init__(
        self,
        api_key: str,
        database_id: str,
        log_service: "AdminLogService",
    ) -> None:
        self._database_id = database_id
        self._log = log_service
        self._client = httpx.AsyncClient(
            base_url=NOTION_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=10.0,
            event_hooks={"request": [_refuse_deletion]},
        )

    async def close(self) -> None:
        """Close the underlying httpx client. Call in bot shutdown."""
        await self._client.aclose()

    async def _write_with_retry(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any],
        context: str,
    ) -> dict[str, Any]:
        """Execute a Notion write with exponential backoff.

        Attempts up to 3 times. Delays between retries: 1s, then 2s.
        On 3rd failure posts ERR_NOTION_WRITE with context to admin log
        and raises NotionWriteError.

        Args:
            method: HTTP method ("POST", "PATCH").
            endpoint: Notion API endpoint path (e.g. "/pages").
            payload: JSON body.
            context: Human-readable description of what was being written
                     (e.g. "booking slot 2026-06-01 20:00 for DJ Axel").

        [Source: architecture.md#Notion Client: exponential back-off (3 retries)]
        [Source: epics.md#Story 1.5 AC2]
        """
        last_exc: Exception | None = None
        for attempt in range(1, 4):  # attempts 1, 2, 3
            try:
                response = await self._client.request(method, endpoint, json=payload)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                logger.warning(
                    "Notion write attempt %d/3 failed for %r: %s",
                    attempt,
                    context,
                    exc,
                )
                if attempt < 3:
                    await asyncio.sleep(_RETRY_DELAYS[attempt - 1])

        # All 3 attempts failed — post to admin log and raise
        await self._log.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger=f"Notion write failed after 3 attempts: {context}",
            action_taken="Bot aborted the write operation.",
            manual_action=f"Check Notion API key and database permissions. Context: {context}",
        )
        raise NotionWriteError(
            f"Notion write failed after 3 attempts: {context}"
        ) from last_exc

    async def validate_schema(self, required_properties: list[str]) -> None:
        """Check that the Notion database has all required properties.

        Fetches the database metadata via GET /databases/{id}.
        For each property in required_properties that is absent from the
        database, logs a descriptive error and posts ERR_NOTION_WRITE to
        the admin log channel.

        This is called once at bot startup (inside on_ready) so that the
        admin log channel is already configured before errors are posted.

        [Source: epics.md#Story 1.5 AC5]
        [Source: architecture.md — NFR13: Notion schema validation on startup]
        """
        try:
            response = await self._client.get(f"/databases/{self._database_id}")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("Failed to fetch Notion database schema: %s", exc)
            await self._log.post_error(
                error_code=ErrorCode.ERR_NOTION_WRITE,
                trigger=f"Could not fetch Notion database schema on startup: {exc}",
                action_taken="Schema validation skipped.",
                manual_action=(
                    "Check NOTION_API_KEY and NOTION_DATABASE_ID. "
                    "Ensure the integration is shared with the database."
                ),
            )
            return

        existing_properties: set[str] = set(data.get("properties", {}).keys())
        for prop in required_properties:
            if prop not in existing_properties:
                logger.error(
                    "Notion database is missing required property: %r (found: %s)",
                    prop,
                    sorted(existing_properties),
                )
                await self._log.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger=f"Notion database missing required property: '{prop}'",
                    action_taken=(
                        "Bot will continue but features depending on this property will fail."
                    ),
                    manual_action=f"Add property '{prop}' to the Notion database schema.",
                )

    async def poll_events(self) -> list[dict[str, Any]]:
        """Fetch all future events from Notion sorted by date ascending.

        Uses POST /databases/{id}/query with a date filter (on_or_after today).
        Follows Notion pagination (has_more / next_cursor) to return ALL pages.
        Returns the complete list of page objects from the Notion API response.

        On HTTP error or network failure: logs a warning and re-raises so the
        caller (EventsCog._poll_notion) can handle error reporting and retry semantics.
        If a later page fails mid-pagination, the exception is raised and the caller
        treats the whole cycle as failed — no partial snapshot update occurs.

        [Source: epics.md#Story 3.1 AC1, AC4]
        """
        today = datetime.date.today().isoformat()
        base_payload: dict[str, Any] = {
            "filter": {
                "property": "Date",
                "date": {"on_or_after": today},
            },
            "sorts": [{"property": "Date", "direction": "ascending"}],
        }

        all_results: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            payload = dict(base_payload)
            if cursor is not None:
                payload["start_cursor"] = cursor
            try:
                response = await self._client.post(
                    f"/databases/{self._database_id}/query",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.warning("Notion poll_events failed: %s", exc)
                raise

            all_results.extend(data.get("results", []))

            if data.get("has_more"):
                cursor = data.get("next_cursor")
            else:
                break

        return all_results

    async def write_dj_record(self, discord_user_id: int, dj_name: str) -> str:
        """Create a DJ page in the Notion database. Returns the new page's Notion ID.

        Properties written: Name (title), Discord ID (rich_text).
        Uses _write_with_retry — posts ERR_NOTION_WRITE and raises NotionWriteError
        if all 3 attempts fail.

        Return value (Notion page ID) is stored in the djs table via
        set_notion_dj_page_id() so BookingConfirmView can later look it up when
        writing a booking to the event's DJs relation.

        Called from OnboardingModal.callback after successful DB insert (Story 2.3).
        Caller catches the exception and continues — Notion sync is best-effort.

        [Source: epics.md#Story 2.3 AC2, AC3]
        [Source: epics.md#Story 4.2 AC2 — DJ Notion page ID prerequisite]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        """
        payload: dict[str, Any] = {
            "parent": {"database_id": self._database_id},
            "properties": {
                "Name": {
                    "title": [{"type": "text", "text": {"content": dj_name}}]
                },
                "Discord ID": {
                    "rich_text": [{"type": "text", "text": {"content": str(discord_user_id)}}]
                },
            },
        }
        result = await self._write_with_retry(
            method="POST",
            endpoint="/pages",
            payload=payload,
            context=f"DJ record for {dj_name} (discord_user_id={discord_user_id})",
        )
        return result["id"]

    async def write_booking(
        self,
        event_id: str,
        slot_index: int,
        dj_notion_page_id: str | None,
        current_dj_ids: list[str],
        event_name: str,
        slot_unix: int,
    ) -> None:
        """Append a DJ to the event's DJs relation in Notion.

        Builds the new DJs list as current_dj_ids + dj_notion_page_id (if not None),
        then PATCHes the event page. This REPLACES the entire relation — including
        existing IDs is mandatory to avoid overwriting prior bookings.

        If dj_notion_page_id is None (DJ onboarded before Story 4.2), the PATCH still
        fires with only the existing IDs. The slot_locks DB row remains the authoritative
        lock; Notion reflects reality minus the late DJ's page reference.

        Uses _write_with_retry — raises NotionWriteError after 3 failed attempts.
        Caller (BookingConfirmView._handle_confirm) must call release_slot_lock() on
        NotionWriteError to undo the DB slot claim.

        [Source: epics.md#Story 4.2 AC2, AC3]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        [Source: architecture.md#Gap Analysis — PG lock vs Notion write boundary]
        """
        new_djs = list(current_dj_ids)
        if dj_notion_page_id is not None:
            new_djs.append(dj_notion_page_id)
        payload: dict[str, Any] = {
            "properties": {
                "DJs": {
                    "relation": [{"id": pid} for pid in new_djs]
                }
            }
        }
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "TBD"
        await self._write_with_retry(
            method="PATCH",
            endpoint=f"/pages/{event_id}",
            payload=payload,
            context=f"booking slot {slot_index + 1} in event '{event_name}' at {slot_display}",
        )

    async def delete_booking(
        self,
        event_id: str,
        dj_notion_page_id: str | None,
        current_dj_ids: list[str],
        event_name: str,
        slot_unix: int,
    ) -> None:
        """Remove a DJ from the event's DJs relation in Notion.

        Builds a new DJs list excluding dj_notion_page_id, then PATCHes the event page.
        If dj_notion_page_id is None (DJ has no Notion page), the PATCH fires with
        current_dj_ids unchanged — best-effort; the slot_locks DB row is the authoritative
        cancellation record.

        Uses _write_with_retry — raises NotionWriteError after 3 failed attempts.
        Caller (CancelConfirmView._handle_confirm) MUST call release_slot_lock()
        regardless of Notion write outcome — the slot must be freed.

        [Source: epics.md#Story 4.4 AC4]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        """
        if dj_notion_page_id is not None:
            new_djs = [pid for pid in current_dj_ids if pid != dj_notion_page_id]
        else:
            new_djs = list(current_dj_ids)
        payload: dict[str, Any] = {
            "properties": {
                "DJs": {
                    "relation": [{"id": pid} for pid in new_djs]
                }
            }
        }
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "TBD"
        await self._write_with_retry(
            method="PATCH",
            endpoint=f"/pages/{event_id}",
            payload=payload,
            context=f"cancellation of slot in event '{event_name}' at {slot_display}",
        )

    async def create_event(
        self,
        event_name: str,
        event_date: datetime.date,
    ) -> str:
        """Create a new event page in the Notion events database.

        Properties written: Name (title), Date (date, ISO date-only, no time component).
        The DJs relation is NOT explicitly set — Notion initialises it empty by default.

        Uses _write_with_retry — posts ERR_NOTION_WRITE and raises NotionWriteError
        if all 3 attempts fail.

        Returns the new Notion page ID (str), stored by callers for later reference.

        Called by EventsCog._auto_create_events() for scheduled auto-creation and
        by AdminCog.add_event() for manual creation via /add-event command.

        [Source: epics.md#Story 7.1 AC2, AC3, AC5]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        """
        payload: dict[str, Any] = {
            "parent": {"database_id": self._database_id},
            "properties": {
                "Name": {
                    "title": [{"type": "text", "text": {"content": event_name}}]
                },
                "Date": {
                    "date": {"start": event_date.isoformat()}
                },
            },
        }
        result = await self._write_with_retry(
            method="POST",
            endpoint="/pages",
            payload=payload,
            context=f"auto event creation for {event_name} on {event_date.isoformat()}",
        )
        return result["id"]
