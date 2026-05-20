"""Tests for bot/notion/client.py — NotionClient retry/backoff and schema validation."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.notion.client import NotionClient, NotionWriteError
from bot.ui.embeds import ErrorCode


def make_mock_log() -> MagicMock:
    log = MagicMock()
    log.post_error = AsyncMock(return_value=None)
    return log


def make_client(log: MagicMock | None = None) -> NotionClient:
    return NotionClient(
        api_key="secret_test",
        database_id="test-db-id",
        log_service=log or make_mock_log(),
    )


@pytest.mark.asyncio
async def test_write_with_retry_succeeds_on_first_attempt() -> None:
    """No error posted when write succeeds on first attempt."""
    log = make_mock_log()
    client = make_client(log)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"id": "page-1"})
    client._client.request = AsyncMock(return_value=mock_response)

    result = await client._write_with_retry("POST", "/pages", {}, "test context")

    assert result == {"id": "page-1"}
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_write_with_retry_does_not_post_error_on_transient_failure() -> None:
    """ERR_NOTION_WRITE is NOT posted when retries eventually succeed."""
    log = make_mock_log()
    client = make_client(log)
    # Fail twice, then succeed
    mock_success = MagicMock()
    mock_success.raise_for_status = MagicMock()
    mock_success.json = MagicMock(return_value={"id": "page-2"})
    client._client.request = AsyncMock(
        side_effect=[
            httpx.RequestError("timeout"),
            httpx.RequestError("timeout"),
            mock_success,
        ]
    )
    with patch("bot.notion.client.asyncio.sleep", new=AsyncMock()):
        result = await client._write_with_retry("POST", "/pages", {}, "ctx")

    assert result == {"id": "page-2"}
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_write_with_retry_posts_error_on_third_failure() -> None:
    """ERR_NOTION_WRITE is posted when all 3 attempts fail, and NotionWriteError is raised."""
    log = make_mock_log()
    client = make_client(log)
    client._client.request = AsyncMock(side_effect=httpx.RequestError("network error"))

    with patch("bot.notion.client.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(NotionWriteError):
            await client._write_with_retry("POST", "/pages", {}, "slot 2026-06-01")

    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE


@pytest.mark.asyncio
async def test_validate_schema_all_present_no_error() -> None:
    """No error posted when all required properties are present."""
    log = make_mock_log()
    client = make_client(log)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={"properties": {"Name": {}, "Date": {}, "Slots": {}}}
    )
    client._client.get = AsyncMock(return_value=mock_response)

    await client.validate_schema(["Name", "Date", "Slots"])

    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_validate_schema_missing_property_posts_error_per_missing() -> None:
    """ERR_NOTION_WRITE posted once per missing property."""
    log = make_mock_log()
    client = make_client(log)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"properties": {"Name": {}}})
    client._client.get = AsyncMock(return_value=mock_response)

    await client.validate_schema(["Name", "Date", "Slots"])

    assert log.post_error.await_count == 2  # Date and Slots are missing
    codes = [c.kwargs["error_code"] for c in log.post_error.call_args_list]
    assert all(code == ErrorCode.ERR_NOTION_WRITE for code in codes)


@pytest.mark.asyncio
async def test_validate_schema_fetch_error_posts_single_error() -> None:
    """A network error fetching the schema results in one ERR_NOTION_WRITE."""
    log = make_mock_log()
    client = make_client(log)
    client._client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))

    await client.validate_schema(["Name", "Date"])

    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE


# ---------------------------------------------------------------------------
# Story 2.3: write_dj_record
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Story 3.1: poll_events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_events_returns_results_list() -> None:
    """poll_events returns the 'results' list from the Notion query response."""
    client = make_client()
    pages = [
        {"id": "page-a", "last_edited_time": "2026-06-01T00:00:00.000Z", "properties": {}},
        {"id": "page-b", "last_edited_time": "2026-06-02T00:00:00.000Z", "properties": {}},
    ]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"results": pages, "has_more": False})
    client._client.post = AsyncMock(return_value=mock_response)

    result = await client.poll_events()

    assert result == pages
    client._client.post.assert_awaited_once()
    call_args = client._client.post.call_args
    assert "/databases/test-db-id/query" in call_args[0][0]


@pytest.mark.asyncio
async def test_poll_events_sends_date_filter_and_sort() -> None:
    """poll_events payload includes on_or_after date filter and ascending date sort."""
    client = make_client()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"results": [], "has_more": False})
    client._client.post = AsyncMock(return_value=mock_response)

    await client.poll_events()

    payload = client._client.post.call_args.kwargs["json"]
    assert payload["filter"]["property"] == "Date"
    assert "on_or_after" in payload["filter"]["date"]
    assert payload["sorts"] == [{"property": "Date", "direction": "ascending"}]


@pytest.mark.asyncio
async def test_poll_events_follows_pagination() -> None:
    """poll_events fetches all pages when Notion returns has_more=True."""
    client = make_client()
    page1 = {"id": "page-a", "last_edited_time": "2026-06-01T00:00:00.000Z"}
    page2 = {"id": "page-b", "last_edited_time": "2026-06-02T00:00:00.000Z"}

    resp1 = MagicMock()
    resp1.raise_for_status = MagicMock()
    resp1.json = MagicMock(return_value={"results": [page1], "has_more": True, "next_cursor": "cursor-1"})

    resp2 = MagicMock()
    resp2.raise_for_status = MagicMock()
    resp2.json = MagicMock(return_value={"results": [page2], "has_more": False})

    client._client.post = AsyncMock(side_effect=[resp1, resp2])

    result = await client.poll_events()

    assert result == [page1, page2]
    assert client._client.post.await_count == 2
    # Second call must include start_cursor
    second_payload = client._client.post.call_args_list[1].kwargs["json"]
    assert second_payload["start_cursor"] == "cursor-1"


@pytest.mark.asyncio
async def test_poll_events_reraises_on_http_error() -> None:
    """poll_events re-raises httpx errors after logging a warning."""
    client = make_client()
    client._client.post = AsyncMock(side_effect=httpx.RequestError("timeout"))

    with pytest.raises(httpx.RequestError):
        await client.poll_events()


@pytest.mark.asyncio
async def test_poll_events_empty_results_returns_empty_list() -> None:
    """poll_events returns [] when Notion response has no results key."""
    client = make_client()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"has_more": False})
    client._client.post = AsyncMock(return_value=mock_response)

    result = await client.poll_events()

    assert result == []


@pytest.mark.asyncio
async def test_write_dj_record_posts_correct_payload() -> None:
    """write_dj_record calls _write_with_retry with correct Notion page payload."""
    log = make_mock_log()
    client = make_client(log)

    with patch.object(client, "_write_with_retry", new=AsyncMock()) as mock_write:
        await client.write_dj_record(discord_user_id=12345, dj_name="TestDJ")

    mock_write.assert_awaited_once()
    call_kwargs = mock_write.call_args.kwargs
    assert call_kwargs["method"] == "POST"
    assert call_kwargs["endpoint"] == "/pages"
    payload = call_kwargs["payload"]
    assert payload["parent"]["database_id"] == "test-db-id"
    assert payload["properties"]["Name"]["title"][0]["text"]["content"] == "TestDJ"
    assert payload["properties"]["Discord ID"]["rich_text"][0]["text"]["content"] == "12345"
