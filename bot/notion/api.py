"""Low-level Notion REST API layer — retry/backoff, pagination.

The API key is only used in the Authorization header and never logged.
"""
import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

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
                "Content-Type": "application/json",
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
    ) -> dict[str, Any]:
        """Send a request with up to 3 attempts (1s, 2s backoff).

        429 honours Retry-After; other 4xx are not retried. A status listed in
        ``tolerate`` raises NotionWriteError(status_code=...) immediately,
        without retries and without posting to the admin log.
        """
        failure = "unknown error"
        last_status: int | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
            retryable = True
            try:
                response = await self._client.request(method, path, json=payload)
                status = response.status_code
                if status < 400:
                    return response.json()
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
                elif status < 500:
                    retryable = False
            except httpx.RequestError as exc:
                failure = type(exc).__name__
                last_status = None
            logger.warning(
                "Notion %s %s attempt %d/%d failed (%s) for %r",
                method, path, attempt, _MAX_ATTEMPTS, failure, context,
            )
            if not retryable:
                break
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(delay)

        await self._log.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger=f"Notion {method} {path} failed ({failure}): {context}",
            action_taken="Bot aborted the operation.",
            manual_action=f"Check Notion API key and permissions. Context: {context}",
        )
        raise NotionWriteError(
            f"Notion {method} {path} failed ({failure}): {context}", last_status
        )

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
