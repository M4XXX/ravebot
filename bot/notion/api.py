"""Low-level Notion REST API layer — retry/backoff, pagination.

The API key is only used in the Authorization header and never logged.
"""
import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from bot.ui import texts
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2026-03-11"
NOTION_BASE_URL = "https://api.notion.com/v1"

_RETRY_DELAYS = (1.0, 2.0)
_MAX_ATTEMPTS = 3


class NotionWriteError(Exception):
    """Raised when a Notion request fails after all retry attempts."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _BadReply(Exception):
    """A 2xx reply that is not JSON (handled inside NotionApi.request)."""


class NotionApi:
    """Async httpx wrapper for the Notion API."""

    def __init__(
        self,
        api_key: str,
        log_service: "AdminLogService",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._log = log_service
        self._client = httpx.AsyncClient(
            base_url=NOTION_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_API_VERSION,
            },
            timeout=10.0,
            transport=transport,
        )

    @property
    def log(self) -> "AdminLogService":
        return self._log

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        context: str,
        *,
        tolerate: tuple[int, ...] = (),
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a request with up to 3 attempts (1s, 2s backoff).

        429 honours Retry-After; other 4xx are not retried. A status listed in
        ``tolerate`` raises NotionWriteError(status_code=...) immediately,
        without retries and without posting to the admin log.
        """
        failure = "unknown error"
        last_status: int | None = None
        # A create (POST that is not a query) may have been saved even though the reply
        # was lost; sending it again would create a duplicate page. It is only retried
        # when Notion surely did not take it: no connection, or 429 (rate limited).
        idempotent = method != "POST" or path.endswith("/query")
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
            retryable = True
            try:
                if files is not None:
                    response = await self._client.request(method, path, files=files)
                else:
                    response = await self._client.request(method, path, json=payload)
                status = response.status_code
                if status < 400:
                    try:
                        return response.json()
                    except ValueError:
                        failure = f"HTTP {status} without a JSON body"
                        last_status = status
                        retryable = False
                        raise _BadReply() from None
                failure = f"HTTP {status}"
                last_status = status
                if status in tolerate:
                    raise NotionWriteError(
                        f"Notion {method} {path} failed ({failure}): {context}",
                        status,
                    )
                if status == 429:
                    try:
                        retry_after = float(response.headers.get("Retry-After", 0))
                    except ValueError:
                        retry_after = 0.0
                    delay = max(delay, retry_after)
                elif status < 500 or not idempotent:
                    retryable = False
            except _BadReply:
                pass
            except httpx.RequestError as exc:
                failure = type(exc).__name__
                last_status = None
                if not idempotent and not isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout)
                ):
                    retryable = False
            logger.warning(
                "Notion %s %s attempt %d/%d failed (%s) for %r",
                method, path, attempt, _MAX_ATTEMPTS, failure, context,
            )
            if not retryable:
                break
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(delay)

        logger.error("Notion %s %s failed (%s): %s", method, path, failure, context)
        if method == "GET" or path.endswith("/query"):
            await self._log.log_attention(
                texts.LOG_FAILED_TITLE,
                "The bot could not read from Notion. It tries again on the next poll.",
                "Check that the Notion integration still has access to the pages.",
            )
        else:
            await self._log.post_error(
                error_code=ErrorCode.ERR_NOTION_WRITE,
                trigger="A change could not be saved to Notion.",
                action_taken="The bot stopped that operation.",
                manual_action=texts.NOTION_RETRY_TODO,
            )
        raise NotionWriteError(
            f"Notion {method} {path} failed ({failure}): {context}", last_status
        )

    async def upload_file(
        self, filename: str, content_type: str, data: bytes, context: str
    ) -> str:
        """Upload a small file to Notion and return the file upload id.

        POST /file_uploads (JSON) then POST /file_uploads/{id}/send as multipart
        form field ``file`` (httpx sets the multipart Content-Type itself; the
        client has no default JSON Content-Type). The id must be attached to a
        page within about an hour.
        """
        created = await self.request(
            "POST",
            "/file_uploads",
            {"filename": filename, "content_type": content_type},
            f"create file upload: {context}",
        )
        upload_id = created["id"]
        await self.request(
            "POST",
            f"/file_uploads/{upload_id}/send",
            None,
            f"send file upload: {context}",
            files={"file": (filename, data, content_type)},
        )
        return upload_id

    async def query_all(
        self, data_source_id: str, payload: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Query a data source, following next_cursor, returning all pages."""
        body = dict(payload or {})
        pages: list[dict[str, Any]] = []
        while True:
            data = await self.request(
                "POST",
                f"/data_sources/{data_source_id}/query",
                body,
                f"query data source {data_source_id}",
            )
            pages.extend(data.get("results", []))
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return pages
            body["start_cursor"] = cursor
