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
from collections.abc import Awaitable, Callable
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

# Failures where the request never reached Notion: always safe to retry.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _retry_after(response: httpx.Response, default: float) -> float:
    """Seconds to wait before retrying a 429, from Notion's Retry-After header."""
    try:
        return max(default, float(response.headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


def _same_page(a: str, b: str) -> bool:
    """Notion page IDs compare equal with or without dashes."""
    return a.replace("-", "").lower() == b.replace("-", "").lower()

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
        # Serialises read-modify-write of each event's DJs relation (one bot process).
        self._event_locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        """Close the underlying httpx client. Call in bot shutdown."""
        await self._client.aclose()

    async def _write_with_retry(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any],
        context: str,
        find_existing: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> dict[str, Any]:
        """Execute a Notion write, retrying only where a retry is safe and can help.

        Up to 3 attempts with 1 s / 2 s backoff. Retried: rate limits (429, honouring
        Retry-After), Notion server errors (5xx) and network errors. Not retried: other
        4xx responses, which a retry cannot fix.

        A timeout after the request was sent may mean Notion already applied it. For
        non-idempotent POSTs the caller passes find_existing; it is consulted before each
        retry so a page that was in fact created is returned instead of created twice.

        After the last failure posts ERR_NOTION_WRITE with context to the admin log and
        raises NotionWriteError.

        [Source: architecture.md#Notion Client: exponential back-off (3 retries)]
        [Source: epics.md#Story 1.5 AC2]
        """
        last_exc: Exception | None = None
        for attempt in range(1, 4):  # attempts 1, 2, 3
            delay = _RETRY_DELAYS[attempt - 1] if attempt < 3 else 0.0
            try:
                response = await self._client.request(method, endpoint, json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if status != 429 and status < 500:
                    logger.warning("Notion write for %r rejected (%d): %s", context, status, exc)
                    break  # a client error does not go away by retrying
                if status == 429:
                    delay = _retry_after(exc.response, delay)
            except httpx.RequestError as exc:
                last_exc = exc
                if find_existing is not None and not isinstance(exc, _NOT_SENT_ERRORS):
                    try:
                        existing = await find_existing()
                    except Exception:  # lookup is best-effort; fall back to retrying
                        logger.exception("Notion lookup after failed write raised")
                        existing = None
                    if existing is not None:
                        logger.info("Notion write for %r had already been applied", context)
                        return existing
            logger.warning(
                "Notion write attempt %d/3 failed for %r: %s",
                attempt,
                context,
                last_exc,
            )
            if attempt < 3:
                await asyncio.sleep(delay)

        # All attempts failed (or a non-retryable error) — post to admin log and raise
        await self._log.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger=f"Notion write failed: {context}",
            action_taken="Bot aborted the write operation.",
            manual_action=f"Check Notion API key and database permissions. Context: {context}",
        )
        raise NotionWriteError(f"Notion write failed: {context}") from last_exc

    async def _find_page(self, notion_filter: dict[str, Any]) -> dict[str, Any] | None:
        """First page of the database matching notion_filter, or None."""
        response = await self._client.post(
            f"/databases/{self._database_id}/query",
            json={"filter": notion_filter, "page_size": 1},
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        return results[0] if results else None

    async def _relation_ids(self, event_id: str, context: str) -> list[str]:
        """Current DJs relation of an event, read fresh from Notion (all pages of it).

        Retries reads like writes; raises NotionWriteError if the relation cannot be read,
        because writing without it would overwrite other DJs' bookings.
        """
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = await self._client.get(f"/pages/{event_id}")
                response.raise_for_status()
                prop = response.json()["properties"]["DJs"]
                if not prop.get("has_more"):
                    return [r["id"] for r in prop.get("relation", [])]
                ids: list[str] = []
                cursor: str | None = None
                while True:
                    params = {"start_cursor": cursor} if cursor else None
                    page = await self._client.get(
                        f"/pages/{event_id}/properties/{prop['id']}", params=params
                    )
                    page.raise_for_status()
                    data = page.json()
                    ids += [item["relation"]["id"] for item in data.get("results", [])]
                    if not data.get("has_more"):
                        return ids
                    cursor = data.get("next_cursor")
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_exc = exc
                logger.warning("Reading DJs of event %s failed (%d/3): %s", event_id, attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(_RETRY_DELAYS[attempt - 1])
        await self._log.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger=f"Could not read the DJs relation before writing: {context}",
            action_taken="Bot aborted the write so other bookings are not overwritten.",
            manual_action="Check Notion API status and the event page.",
        )
        raise NotionWriteError(f"Could not read DJs relation: {context}") from last_exc

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
            find_existing=lambda: self._find_page(
                {"property": "Discord ID", "rich_text": {"equals": str(discord_user_id)}}
            ),
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
        """Add a DJ to the event's DJs relation in Notion.

        The relation is re-read from Notion under a per-event lock right before the PATCH
        and only this DJ's page is added. A PATCH replaces the whole relation, so building
        it from a stale copy (current_dj_ids, kept for call compatibility and ignored)
        silently dropped other DJs who booked meanwhile or were added by hand.

        If dj_notion_page_id is None (DJ onboarded before Story 4.2) nothing is written.
        The slot_locks DB row remains the authoritative lock.

        Uses _write_with_retry — raises NotionWriteError after 3 failed attempts.
        Caller (BookingConfirmView._handle_confirm) must call release_slot_lock() on
        NotionWriteError to undo the DB slot claim.

        [Source: epics.md#Story 4.2 AC2, AC3]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        [Source: architecture.md#Gap Analysis — PG lock vs Notion write boundary]
        """
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "TBD"
        context = f"booking slot {slot_index + 1} in event '{event_name}' at {slot_display}"
        if dj_notion_page_id is None:
            return
        async with self._event_locks.setdefault(event_id, asyncio.Lock()):
            new_djs = await self._relation_ids(event_id, context)
            if any(_same_page(pid, dj_notion_page_id) for pid in new_djs):
                return  # already linked
            new_djs.append(dj_notion_page_id)
            payload: dict[str, Any] = {
                "properties": {"DJs": {"relation": [{"id": pid} for pid in new_djs]}}
            }
            await self._write_with_retry(
                method="PATCH",
                endpoint=f"/pages/{event_id}",
                payload=payload,
                context=context,
            )

    async def delete_booking(
        self,
        event_id: str,
        dj_notion_page_id: str | None,
        current_dj_ids: list[str],
        event_name: str,
        slot_unix: int,
    ) -> None:
        """Remove this DJ (and only this DJ) from the event's DJs relation in Notion.

        Re-reads the relation under the per-event lock and PATCHes it without this DJ's
        page; every other entry stays. current_dj_ids is kept for call compatibility and
        ignored. If dj_notion_page_id is None nothing is written. The slot_locks DB row is
        the authoritative cancellation record.

        Uses _write_with_retry — raises NotionWriteError after 3 failed attempts.
        Caller (CancelConfirmView._handle_confirm) MUST call release_slot_lock()
        regardless of Notion write outcome — the slot must be freed.

        [Source: epics.md#Story 4.4 AC4]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        """
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "TBD"
        context = f"cancellation of slot in event '{event_name}' at {slot_display}"
        if dj_notion_page_id is None:
            return  # nothing of ours to remove; never rewrite other DJs' entries
        async with self._event_locks.setdefault(event_id, asyncio.Lock()):
            current = await self._relation_ids(event_id, context)
            new_djs = [pid for pid in current if not _same_page(pid, dj_notion_page_id)]
            if len(new_djs) == len(current):
                return  # already gone
            payload: dict[str, Any] = {
                "properties": {"DJs": {"relation": [{"id": pid} for pid in new_djs]}}
            }
            await self._write_with_retry(
                method="PATCH",
                endpoint=f"/pages/{event_id}",
                payload=payload,
                context=context,
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
            find_existing=lambda: self._find_page({"and": [
                {"property": "Name", "title": {"equals": event_name}},
                {"property": "Date", "date": {"equals": event_date.isoformat()}},
            ]}),
        )
        return result["id"]
