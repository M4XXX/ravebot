"""Notion REST API client — exponential backoff, schema validation.

Story 1.5 scope: NotionWriteError, NotionClient with _write_with_retry() and validate_schema().
Story 3.1 scope: poll_events() — fetch future events for change detection.
Story 7.1 scope: create_event() — auto-create and manual event creation in Notion.
Full read_slots(), write_booking(), delete_booking() are Story 3.x scope.

[Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
[Source: architecture.md#Error Routing: Centralized AdminLogService]
"""
import asyncio
import contextlib
import datetime
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from bot import slots
from bot.notion import props
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Retry delays in seconds: attempt 1 → wait 1s, attempt 2 → wait 2s, attempt 3 → fail
_RETRY_DELAYS = (1.0, 2.0)

# Longest Retry-After the bot honours. A DJ is waiting on the answer, and every attempt
# re-reads what it writes, so retrying a little early costs at most one more 429.
_RETRY_AFTER_CAP = 10.0

# Failures where the request never reached Notion: always safe to retry.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _retry_after(response: httpx.Response, default: float) -> float:
    """Seconds to wait before retrying a 429, from Notion's Retry-After header."""
    try:
        wait = max(default, float(response.headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default
    return min(wait, _RETRY_AFTER_CAP)


# How the Notion UI names the property types the bot checks (validate_schema messages)
_NOTION_TYPE_LABELS = {
    "title": "Title",
    "rich_text": "Text",
    "number": "Number",
    "date": "Date",
    "relation": "Relation",
    "status": "Status",
    "select": "Select",
}


def _is_dj_page(page: dict[str, Any], dj_pages_here: bool) -> bool:
    """True for a page that is not an event: no Date, or (shared database) a Discord ID."""
    found = props.find_property(page, "Date")
    date = (found[1].get("date") if found else None) or {}
    if not date.get("start"):
        return True
    if dj_pages_here:
        discord_id = props.find_property(page, "Discord ID")
        return bool(discord_id and props.plain_text(discord_id[1]))
    return False


def _same_page(a: str, b: str) -> bool:
    """Notion page IDs compare equal with or without dashes."""
    return a.replace("-", "").lower() == b.replace("-", "").lower()

# Body keys Notion reads to move a page, block or database to the trash.
_TRASH_KEYS = ("archived", "in_trash", "is_archived")


class NotionWriteError(Exception):
    """Raised when a Notion write fails after all retry attempts."""


class NotionSlotTakenError(NotionWriteError):
    """Raised when a slot's column already holds a name the bot did not write (tds layout).

    The bot never overwrites what an organizer typed into the plan.
    """

    def __init__(self, column: str, current: str) -> None:
        super().__init__(f"Slot column {column!r} already says {current!r}")
        self.column = column
        self.current = current


class NotionNotBookableError(NotionWriteError):
    """Raised when the event left the bookable statuses before the name was written (tds)."""

    def __init__(self, status: str | None) -> None:
        super().__init__(f"Event status {status!r} is not open for booking")
        self.status = status


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


def _removed_content(method: str, path: str, payload: Any) -> str | None:
    """What a PATCH would wipe besides trashing, or None (creating a page removes nothing).

    Dropping a database property (setting it to null) deletes that column with all its
    values, and an empty title blanks a page or database name. Emptying a relation is
    allowed: it is how the last DJ of an event is unlinked, and the bot only ever removes
    its own entry from a freshly read relation.
    """
    if method != "PATCH" or not isinstance(payload, dict):
        return None
    is_database = "/databases/" in path
    if is_database and payload.get("title") == []:
        return "blanks the database title"
    props = payload.get("properties")
    if not isinstance(props, dict):
        return None
    for name, value in props.items():
        if value is None and is_database:
            return f"drops the database property {name!r}"
        if isinstance(value, dict) and value.get("title") == []:
            return f"blanks the title {name!r}"
    return None


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
    removed = _removed_content(request.method, request.url.path, payload)
    if removed is not None:
        logger.error(
            "Blocked Notion %s %s that %s — deletion is human-only",
            request.method,
            request.url.path,
            removed,
        )
        raise NotionDeletionBlockedError(f"{request.method} {request.url.path} {removed}")


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
        layout: str = slots.CLASSIC,
        new_event_status: str | None = None,
        dj_database_id: str | None = None,
    ) -> None:
        self._database_id = database_id
        # DJ profile pages (classic layout): their own database, or the events database
        self._dj_database_id = dj_database_id or database_id
        self._log = log_service
        self.layout = layout
        self._new_event_status = new_event_status
        self._title_name: str | None = None
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
        # Serialises read-modify-write of each event's DJs relation. Only within this
        # process; the single-instance lock (bot/db/instance_lock.py) keeps a second
        # process away. Entries are counted and dropped once no writer holds or awaits them.
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._event_lock_users: dict[str, int] = {}

    @contextlib.asynccontextmanager
    async def _event_lock(self, event_id: str) -> AsyncIterator[None]:
        """Hold the per-event write lock; the entry is removed when nobody uses it."""
        # Counted before awaiting, so a waiting writer keeps the lock alive
        self._event_lock_users[event_id] = self._event_lock_users.get(event_id, 0) + 1
        lock = self._event_locks.setdefault(event_id, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            remaining = self._event_lock_users[event_id] - 1
            if remaining:
                self._event_lock_users[event_id] = remaining
            else:
                del self._event_lock_users[event_id]
                del self._event_locks[event_id]

    @property
    def separate_dj_database(self) -> bool:
        """True when DJ pages live in their own database (NOTION_DJ_DATABASE_ID)."""
        return not _same_page(self._dj_database_id, self._database_id)

    @property
    def per_slot(self) -> bool:
        """True when each slot is written on its own (tds layout: one column per slot).

        In the classic layout a DJ is linked once per event, so the link stays while they
        hold another slot of it.
        """
        return self.layout == slots.TDS

    async def _title_property(self) -> str:
        """Name of the database's title column ("Name" in the classic layout), cached."""
        if self._title_name is None:
            try:
                response = await self._client.get(f"/databases/{self._database_id}")
                response.raise_for_status()
                found = [
                    k for k, v in response.json().get("properties", {}).items()
                    if v.get("type") == "title"
                ]
                self._title_name = found[0] if found else "Name"
            except (httpx.HTTPError, ValueError):
                return "Name"  # not cached: the next call asks again
        return self._title_name

    async def _read_page(self, event_id: str, context: str) -> dict[str, Any]:
        """The event page, read fresh from Notion; retried like the relation read.

        Raises NotionWriteError when it cannot be read: writing without it could overwrite
        a name an organizer typed in meanwhile.
        """
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = await self._client.get(f"/pages/{event_id}")
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                logger.warning("Reading event %s failed (%d/3): %s", event_id, attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(_RETRY_DELAYS[attempt - 1])
        await self._log.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger=f"Could not read the event before writing: {context}",
            action_taken="Bot aborted the write so nothing typed in Notion is overwritten.",
            manual_action="Check Notion API status and the event page.",
        )
        raise NotionWriteError(f"Could not read event page: {context}") from last_exc

    async def _slot_column(
        self, event_id: str, slot_index: int, context: str
    ) -> tuple[str, str, dict[str, Any]]:
        """(real column name, current text, page) of the slot's column, read fresh."""
        page = await self._read_page(event_id, context)
        wanted = slots.column_name(slot_index)
        found = props.find_property(page, wanted)
        if found is None:
            await self._log.post_error(
                error_code=ErrorCode.ERR_NOTION_WRITE,
                trigger=f"The event has no column {wanted!r}: {context}",
                action_taken="Bot aborted the write.",
                manual_action=(
                    f"Add a text column {wanted!r} to the plan, or check /config set start-time "
                    "and slot-duration."
                ),
            )
            raise NotionWriteError(f"No column {wanted!r}: {context}")
        return found[0], props.plain_text(found[1]), page

    async def close(self) -> None:
        """Close the underlying httpx client. Call in bot shutdown."""
        await self._client.aclose()

    async def _write_with_retry(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | Callable[[], Awaitable[dict[str, Any] | None]],
        context: str,
        find_existing: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
        manual_action: str | None = None,
    ) -> dict[str, Any]:
        """Execute a Notion write, retrying only where a retry is safe and can help.

        Up to 3 attempts with 1 s / 2 s backoff. Retried: rate limits (429, honouring
        Retry-After), Notion server errors (5xx) and network errors. Not retried: other
        4xx responses, which a retry cannot fix.

        payload may be an async builder instead of a dict. It is called before every
        attempt, so a retry never re-sends data read before the wait (a relation edited by
        hand meanwhile would be overwritten). A builder returning None means there is
        nothing left to write, e.g. because an earlier attempt was applied after all.

        A timeout or a 5xx after the request was sent may mean Notion already applied it.
        For non-idempotent POSTs the caller passes find_existing; it is consulted before
        each retry so a page that was in fact created is returned instead of created twice.

        After the last failure posts ERR_NOTION_WRITE with context to the admin log and
        raises NotionWriteError. manual_action, if given, replaces the default hint there.

        [Source: architecture.md#Notion Client: exponential back-off (3 retries)]
        [Source: epics.md#Story 1.5 AC2]
        """
        last_exc: Exception | None = None
        for attempt in range(1, 4):  # attempts 1, 2, 3
            delay = _RETRY_DELAYS[attempt - 1] if attempt < 3 else 0.0
            may_be_applied = False
            try:
                body = await payload() if callable(payload) else payload
                if body is None:
                    return {}
                response = await self._client.request(method, endpoint, json=body)
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
                else:
                    may_be_applied = True  # a 502/504 can arrive after the write went through
            except httpx.RequestError as exc:
                last_exc = exc
                may_be_applied = not isinstance(exc, _NOT_SENT_ERRORS)
            if may_be_applied and find_existing is not None:
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
            manual_action=manual_action
            or f"Check Notion API key and database permissions. Context: {context}",
        )
        raise NotionWriteError(f"Notion write failed: {context}") from last_exc

    async def _find_page(
        self, notion_filter: dict[str, Any], database_id: str | None = None
    ) -> dict[str, Any] | None:
        """First page of the database (default: events) matching notion_filter, or None."""
        response = await self._client.post(
            f"/databases/{database_id or self._database_id}/query",
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

    def relation_targets(self) -> dict[str, str]:
        """Relation properties and the database they must point to (validate_schema).

        classic: the DJs relation links DJ pages, which write_dj_record() creates in the DJ
        database (the events database itself unless NOTION_DJ_DATABASE_ID is set,
        docs/user-guide.md 2.4). tds: no relations are used.
        """
        return {} if self.layout == slots.TDS else {"DJs": self._dj_database_id}

    async def validate_schema(
        self,
        required_properties: Mapping[str, Sequence[str]],
        relation_targets: Mapping[str, str] | None = None,
        database_id: str | None = None,
    ) -> None:
        """Check that the Notion database has all required properties, of the right type.

        Fetches the database metadata via GET /databases/{id}. required_properties maps
        each name to its allowed Notion types (bot/slots.py required_properties()). Each
        property that is absent, or present with another type, is logged and posted as
        one ERR_NOTION_WRITE to the admin log channel, naming the fix. A relation named in
        relation_targets must also point to that database (else bookings fail with 400).
        database_id selects the database (default: the events database); messages name it.

        This is called once at bot startup (inside on_ready) so that the
        admin log channel is already configured before errors are posted.

        [Source: epics.md#Story 1.5 AC5]
        [Source: architecture.md — NFR13: Notion schema validation on startup]
        [Source: epics.md#Story 8.4]
        """
        database_id = database_id or self._database_id
        where = (
            "DJ database" if _same_page(database_id, self._dj_database_id)
            and self.separate_dj_database else "events database"
        )
        try:
            response = await self._client.get(f"/databases/{database_id}")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("Failed to fetch Notion database schema: %s", exc)
            await self._log.post_error(
                error_code=ErrorCode.ERR_NOTION_WRITE,
                trigger=f"Could not fetch the Notion {where} schema on startup: {exc}",
                action_taken="Schema validation skipped.",
                manual_action=(
                    "Check NOTION_API_KEY, NOTION_DATABASE_ID and NOTION_DJ_DATABASE_ID. "
                    "Ensure the integration is shared with the database."
                ),
            )
            return

        existing_properties: set[str] = set(data.get("properties", {}).keys())
        for prop, allowed in required_properties.items():
            found = props.find_property(data, prop)
            if found is None:
                logger.error(
                    "Notion database is missing required property: %r (found: %s)",
                    prop,
                    sorted(existing_properties),
                )
                await self._log.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger=f"Notion {where} missing required property: '{prop}'",
                    action_taken=(
                        "Bot will continue but features depending on this property will fail."
                    ),
                    manual_action=f"Add property '{prop}' to the Notion {where}.",
                )
                continue
            real_name, schema = found
            if schema.get("type") not in allowed:
                await self._report_wrong_type(
                    prop, real_name, schema.get("type"), allowed, where
                )
                continue
            target = (relation_targets or {}).get(prop)
            linked = (schema.get("relation") or {}).get("database_id")
            if target and schema.get("type") == "relation" and not (
                linked and _same_page(linked, target)
            ):
                logger.error(
                    "Notion relation %r points to database %s, expected %s",
                    real_name, linked, target,
                )
                await self._log.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger=(
                        f"Notion relation '{real_name}' points to another database "
                        f"({linked}), expected {target}"
                    ),
                    action_taken="Bookings will fail to link DJs in Notion.",
                    manual_action=(
                        f"Edit '{real_name}' in Notion so it relates to the database the bot "
                        "keeps DJ pages in (see docs/user-guide.md 2.4)."
                    ),
                )

    async def validate_dj_schema(self) -> None:
        """Check the separate DJ database (Name, Discord ID); nothing to do in shared mode."""
        if self.layout != slots.TDS and self.separate_dj_database:
            await self.validate_schema(slots.DJ_PROPERTIES, database_id=self._dj_database_id)

    async def _report_wrong_type(
        self,
        name: str,
        real_name: str,
        actual: Any,
        allowed: Sequence[str],
        where: str = "events database",
    ) -> None:
        expected = " or ".join(allowed)
        shown = " or ".join(f'"{_NOTION_TYPE_LABELS.get(t, t)}"' for t in allowed)
        logger.error(
            "Notion property %r has type %r, expected %s", real_name, actual, expected
        )
        await self._log.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger=(
                f"Notion property '{real_name}' in the {where} has type '{actual}', "
                f"expected {expected}"
            ),
            action_taken="Bot will continue but features depending on this property will fail.",
            manual_action=(
                f"Change the type of '{real_name}' in the Notion {where} to {shown}, or "
                f"rename it and "
                f"add a new {shown} property named '{name}'."
            ),
        )

    async def validate_status_names(self, names: list[str]) -> None:
        """Report configured status names the database's Status property does not offer.

        A misspelt name (e.g. "Planning-stage" for "Planing-stage") would silently make
        every event unbookable, and events the bot creates would be rejected by Notion.
        """
        if not names:
            return
        try:
            response = await self._client.get(f"/databases/{self._database_id}")
            response.raise_for_status()
            found = props.find_property(response.json(), "Status")
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("Could not read the Status options: %s", exc)
            return
        if found is None:
            return  # validate_schema reports the missing property
        kind = found[1].get("type", "")
        options = {o.get("name") for o in found[1].get(kind, {}).get("options", [])}
        unknown = [n for n in names if n not in options]
        if unknown:
            logger.error("Status names not in the database: %s (offered: %s)",
                         unknown, sorted(options))
            await self._log.post_error(
                error_code=ErrorCode.ERR_NOTION_WRITE,
                trigger=f"Status names the Notion database does not offer: {', '.join(unknown)}",
                action_taken="Events with these statuses are never matched.",
                manual_action=(
                    "Check NOTION_BOOKABLE_STATUSES and NOTION_CANCELLED_STATUSES against the "
                    "Status options in Notion (exact spelling and case)."
                ),
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

        DJ profile pages are never events: pages without a Date are dropped, and when DJ
        pages share the events database (classic layout, no NOTION_DJ_DATABASE_ID) so
        are pages with a Discord ID — in the query and again here.

        [Source: epics.md#Story 3.1 AC1, AC4]
        [Source: epics.md#Story 8.5]
        """
        today = datetime.date.today().isoformat()
        date_filter = {"property": "Date", "date": {"on_or_after": today}}
        # Only filter on Discord ID where it exists: a filter on a missing property is a 400
        shared = self._dj_pages_in_events_database()
        base_payload: dict[str, Any] = {
            "filter": (
                {"and": [date_filter, {"property": "Discord ID", "rich_text": {"is_empty": True}}]}
                if shared else date_filter
            ),
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

        events = [p for p in all_results if not _is_dj_page(p, shared)]
        if len(events) != len(all_results):
            logger.debug("poll_events dropped %d non-event pages", len(all_results) - len(events))
        return events

    def _dj_pages_in_events_database(self) -> bool:
        return self.layout != slots.TDS and not self.separate_dj_database

    async def write_dj_record(
        self,
        discord_user_id: int,
        dj_name: str,
        context: str | None = None,
        manual_action: str | None = None,
    ) -> str | None:
        """Find or create the DJ's page in the Notion database. Returns its Notion ID.

        An existing page with this Discord ID is returned instead of creating another one
        (a resubmitted profile or a booking must not duplicate it). If that lookup fails
        the page is created anyway — a rare duplicate is better than no page.

        In the tds layout DJ profiles live only in the bot's database and nothing is
        written (returns None): the plan holds events, the DJs appear as names in slots.

        Properties written: Name (title), Discord ID (rich_text).
        Uses _write_with_retry — posts ERR_NOTION_WRITE and raises NotionWriteError
        if all 3 attempts fail.

        Return value (Notion page ID) is stored in the djs table via
        set_notion_dj_page_id() so BookingConfirmView can later look it up when
        writing a booking to the event's DJs relation.

        Called from OnboardingModal.callback after successful DB insert (Story 2.3), and
        through ensure_dj_page() when a DJ without a page books (Story 8.3); context and
        manual_action then name the event in the admin log entry.
        Caller catches the exception and continues — Notion sync is best-effort.

        [Source: epics.md#Story 2.3 AC2, AC3]
        [Source: epics.md#Story 4.2 AC2 — DJ Notion page ID prerequisite]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        """
        if self.layout == slots.TDS:
            return None
        try:
            existing = await self._find_dj_page(discord_user_id)
        except Exception as exc:  # best-effort, like find_existing below
            logger.warning("Looking up the Notion page of %s failed: %s", dj_name, exc)
            existing = None
        if existing is not None:
            return existing["id"]
        payload: dict[str, Any] = {
            "parent": {"database_id": self._dj_database_id},
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
            context=context or f"DJ record for {dj_name} (discord_user_id={discord_user_id})",
            find_existing=lambda: self._find_dj_page(discord_user_id),
            manual_action=manual_action,
        )
        return result["id"]

    async def dj_page_in_dj_database(self, page_id: str) -> bool:
        """False if a stored DJ page ID is not in the DJ database (e.g. a page from before
        NOTION_DJ_DATABASE_ID was set, or a deleted one): linking it would fail with 400.

        Shared mode: True without a request. A failed lookup also answers True — the write
        that follows reports a real problem itself. [Source: story 8.5 review]
        """
        if not self.separate_dj_database:
            return True
        try:
            response = await self._client.get(f"/pages/{page_id}")
            if response.status_code == 404:
                return False
            response.raise_for_status()
            parent = response.json().get("parent", {})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Could not check the Notion page %s of a DJ: %s", page_id, exc)
            return True
        linked = parent.get("database_id")
        return bool(linked) and _same_page(linked, self._dj_database_id)

    def _find_dj_page(self, discord_user_id: int) -> Awaitable[dict[str, Any] | None]:
        return self._find_page(
            {"property": "Discord ID", "rich_text": {"equals": str(discord_user_id)}},
            database_id=self._dj_database_id,
        )

    async def ensure_dj_page(
        self, discord_user_id: int, dj_name: str, event_name: str
    ) -> str | None:
        """The DJ's Notion page ID for a booking: found by Discord ID, else created.

        tds layout: None without any request (no DJ pages, the plan holds names).
        Returns None if the page can neither be found nor created; exactly one
        ERR_NOTION_WRITE then names the DJ and the event. Never raises.

        [Source: epics.md#Story 8.3]
        """
        if self.layout == slots.TDS:
            return None
        context = (
            f"DJ page for {dj_name} (discord_user_id={discord_user_id}), "
            f"needed for the booking in '{event_name}'"
        )
        manual_action = (
            f"Create a page for {dj_name} in Notion and add it to the DJs of "
            f"'{event_name}'. The booking itself is saved."
        )
        try:
            return await self.write_dj_record(
                discord_user_id, dj_name, context=context, manual_action=manual_action
            )
        except NotionWriteError:
            return None  # _write_with_retry has posted the error
        except Exception as exc:  # an unexpected answer; nothing was posted yet
            logger.warning("Finding or creating the Notion page of %s failed: %s", dj_name, exc)
            await self._log.post_error(
                error_code=ErrorCode.ERR_NOTION_WRITE,
                trigger=f"Notion write failed: {context}",
                action_taken="Booking kept without a link in Notion.",
                manual_action=manual_action,
            )
            return None

    async def write_booking(
        self,
        event_id: str,
        slot_index: int,
        dj_notion_page_id: str | None,
        current_dj_ids: list[str],
        event_name: str,
        slot_unix: int,
        dj_name: str | None = None,
        replaces: str | None = None,
        require_bookable: bool = False,
    ) -> None:
        """Add a DJ to the event's DJs relation in Notion.

        In the tds layout the DJ's name is written into the slot's column instead, after a
        fresh read. The cell must be empty, or hold `replaces` (the text the bot wrote for a
        DJ an organizer displaced with /admin book); anything else raises
        NotionSlotTakenError, even the same name typed in by hand. With require_bookable the
        fresh page must also still be in a bookable status (NotionNotBookableError).

        The relation is re-read from Notion under a per-event lock right before every PATCH
        attempt and only this DJ's page is added. A PATCH replaces the whole relation, so building
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
        if self.layout == slots.TDS:
            await self._write_slot_name(
                event_id, slot_index, dj_name, replaces, require_bookable, context
            )
            return
        if dj_notion_page_id is None:
            return

        async def payload() -> dict[str, Any] | None:
            new_djs = await self._relation_ids(event_id, context)
            if any(_same_page(pid, dj_notion_page_id) for pid in new_djs):
                return None  # already linked
            new_djs.append(dj_notion_page_id)
            return {"properties": {"DJs": {"relation": [{"id": pid} for pid in new_djs]}}}

        async with self._event_lock(event_id):
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
        slot_index: int | None = None,
        dj_name: str | None = None,
    ) -> None:
        """Remove this DJ (and only this DJ) from the event's DJs relation in Notion.

        In the tds layout the slot's column is emptied instead — only while it still holds
        exactly this DJ's name; anything an organizer changed it to stays.

        Re-reads the relation under the per-event lock before every attempt and PATCHes it
        without this DJ's page; every other entry stays. current_dj_ids is kept for call
        compatibility and ignored. If dj_notion_page_id is None nothing is written. The
        slot_locks DB row is the authoritative cancellation record.

        Uses _write_with_retry — raises NotionWriteError after 3 failed attempts.
        Caller (CancelConfirmView._handle_confirm) MUST call release_slot_lock()
        regardless of Notion write outcome — the slot must be freed.

        [Source: epics.md#Story 4.4 AC4]
        [Source: architecture.md#Notion Client: Raw httpx (async, HTTPS/SSL)]
        """
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "TBD"
        context = f"cancellation of slot in event '{event_name}' at {slot_display}"
        if self.layout == slots.TDS:
            if slot_index is not None and dj_name:
                await self._clear_slot_name(event_id, slot_index, dj_name, context)
            return
        if dj_notion_page_id is None:
            return  # nothing of ours to remove; never rewrite other DJs' entries

        async def payload() -> dict[str, Any] | None:
            current = await self._relation_ids(event_id, context)
            new_djs = [pid for pid in current if not _same_page(pid, dj_notion_page_id)]
            if len(new_djs) == len(current):
                return None  # already gone
            return {"properties": {"DJs": {"relation": [{"id": pid} for pid in new_djs]}}}

        async with self._event_lock(event_id):
            await self._write_with_retry(
                method="PATCH",
                endpoint=f"/pages/{event_id}",
                payload=payload,
                context=context,
            )

    async def _write_slot_name(
        self,
        event_id: str,
        slot_index: int,
        dj_name: str | None,
        replaces: str | None,
        require_bookable: bool,
        context: str,
    ) -> None:
        """Write dj_name into the slot's column (tds layout), see write_booking."""
        if not dj_name:
            raise NotionWriteError(f"No DJ name to write: {context}")
        sent = False  # a PATCH went out; its name in the cell may be our own then

        async def payload() -> dict[str, Any] | None:
            nonlocal sent
            column, current, page = await self._slot_column(event_id, slot_index, context)
            if sent and current == dj_name:
                return None  # an earlier attempt went through after all
            if current and current != replaces:
                raise NotionSlotTakenError(column, current)
            if require_bookable and not slots.is_bookable(page):
                raise NotionNotBookableError(props.status_name(page))
            sent = True
            return {"properties": {column: {
                "rich_text": [{"type": "text", "text": {"content": dj_name}}]
            }}}

        async with self._event_lock(event_id):
            await self._write_with_retry(
                method="PATCH", endpoint=f"/pages/{event_id}", payload=payload, context=context
            )

    async def _clear_slot_name(
        self, event_id: str, slot_index: int, dj_name: str, context: str
    ) -> None:
        """Empty the slot's column if it still says dj_name (tds layout), see delete_booking."""

        async def payload() -> dict[str, Any] | None:
            column, current, _ = await self._slot_column(event_id, slot_index, context)
            if current != dj_name:
                if current:
                    logger.info(
                        "Not clearing %r of %s: it says %r, not %r", column, event_id,
                        current, dj_name,
                    )
                return None
            return {"properties": {column: {"rich_text": []}}}

        async with self._event_lock(event_id):
            await self._write_with_retry(
                method="PATCH", endpoint=f"/pages/{event_id}", payload=payload, context=context
            )

    async def create_event(
        self,
        event_name: str,
        event_date: datetime.date,
        slots: int | None = None,
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
        slot_count = slots  # the parameter shadows the bot.slots module in here
        title = await self._title_property() if self.per_slot else "Name"
        payload: dict[str, Any] = {
            "parent": {"database_id": self._database_id},
            "properties": {
                title: {
                    "title": [{"type": "text", "text": {"content": event_name}}]
                },
                "Date": {
                    "date": {"start": event_date.isoformat()}
                },
            },
        }
        if self.per_slot:
            if self._new_event_status:  # a new event starts in the planning phase
                payload["properties"]["Status"] = {"status": {"name": self._new_event_status}}
        elif slot_count is not None:
            payload["properties"]["Slots"] = {"number": slot_count}
        result = await self._write_with_retry(
            method="POST",
            endpoint="/pages",
            payload=payload,
            context=f"auto event creation for {event_name} on {event_date.isoformat()}",
            find_existing=lambda: self._find_page({"and": [
                {"property": title, "title": {"equals": event_name}},
                {"property": "Date", "date": {"equals": event_date.isoformat()}},
            ]}),
        )
        return result["id"]
