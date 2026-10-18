"""Tests for bot/notion/client.py — NotionClient retry/backoff and schema validation."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.notion.client import NotionClient, NotionWriteError
from bot.ui.embeds import ErrorCode

_DATED = {"Date": {"type": "date", "date": {"start": "2026-06-14"}}}


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
    mock_response.json = MagicMock(return_value={"properties": {
        "Name": {"type": "title"}, "Date": {"type": "date"}, "Slots": {"type": "number"},
    }})
    client._client.get = AsyncMock(return_value=mock_response)

    await client.validate_schema(
        {"Name": ("title",), "Date": ("date",), "Slots": ("number",)}
    )

    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_validate_schema_missing_property_posts_error_per_missing() -> None:
    """ERR_NOTION_WRITE posted once per missing property."""
    log = make_mock_log()
    client = make_client(log)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"properties": {"Name": {"type": "title"}}})
    client._client.get = AsyncMock(return_value=mock_response)

    await client.validate_schema(
        {"Name": ("title",), "Date": ("date",), "Slots": ("number",)}
    )

    assert log.post_error.await_count == 2  # Date and Slots are missing
    codes = [c.kwargs["error_code"] for c in log.post_error.call_args_list]
    assert all(code == ErrorCode.ERR_NOTION_WRITE for code in codes)


@pytest.mark.asyncio
async def test_validate_schema_fetch_error_posts_single_error() -> None:
    """A network error fetching the schema results in one ERR_NOTION_WRITE."""
    log = make_mock_log()
    client = make_client(log)
    client._client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))

    await client.validate_schema({"Name": ("title",), "Date": ("date",)})

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
        {"id": "page-a", "last_edited_time": "2026-06-01T00:00:00.000Z", "properties": _DATED},
        {"id": "page-b", "last_edited_time": "2026-06-02T00:00:00.000Z", "properties": _DATED},
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
    date_filter, dj_filter = payload["filter"]["and"]  # classic, DJ pages share the database
    assert date_filter["property"] == "Date"
    assert "on_or_after" in date_filter["date"]
    assert dj_filter == {"property": "Discord ID", "rich_text": {"is_empty": True}}
    assert payload["sorts"] == [{"property": "Date", "direction": "ascending"}]


@pytest.mark.asyncio
async def test_poll_events_follows_pagination() -> None:
    """poll_events fetches all pages when Notion returns has_more=True."""
    client = make_client()
    page1 = {"id": "page-a", "last_edited_time": "2026-06-01T00:00:00.000Z", "properties": _DATED}
    page2 = {"id": "page-b", "last_edited_time": "2026-06-02T00:00:00.000Z", "properties": _DATED}

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

    with patch.object(client, "_find_dj_page", new=AsyncMock(return_value=None)), \
         patch.object(client, "_write_with_retry", new=AsyncMock()) as mock_write:
        await client.write_dj_record(discord_user_id=12345, dj_name="TestDJ")

    mock_write.assert_awaited_once()
    call_kwargs = mock_write.call_args.kwargs
    assert call_kwargs["method"] == "POST"
    assert call_kwargs["endpoint"] == "/pages"
    payload = call_kwargs["payload"]
    assert payload["parent"]["database_id"] == "test-db-id"
    assert payload["properties"]["Name"]["title"][0]["text"]["content"] == "TestDJ"
    assert payload["properties"]["Discord ID"]["rich_text"][0]["text"]["content"] == "12345"


# ---------------------------------------------------------------------------
# Story 7.1: create_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_event_posts_correct_payload() -> None:
    """create_event calls _write_with_retry with correct POST /pages payload."""
    import datetime
    from unittest.mock import patch

    client = make_client()
    with patch.object(
        client, "_write_with_retry", new=AsyncMock(return_value={"id": "new-evt-id"})
    ) as mock_write:
        result = await client.create_event(
            "Rave Night 14 Jun 2026", datetime.date(2026, 6, 14)
        )

    assert result == "new-evt-id"
    mock_write.assert_awaited_once()
    call_kwargs = mock_write.call_args.kwargs
    assert call_kwargs["method"] == "POST"
    assert call_kwargs["endpoint"] == "/pages"
    payload = call_kwargs["payload"]
    assert payload["parent"]["database_id"] == "test-db-id"
    assert payload["properties"]["Name"]["title"][0]["text"]["content"] == "Rave Night 14 Jun 2026"
    assert payload["properties"]["Date"]["date"]["start"] == "2026-06-14"


@pytest.mark.asyncio
async def test_create_event_propagates_notion_write_error() -> None:
    """create_event propagates NotionWriteError from _write_with_retry."""
    import datetime
    from unittest.mock import patch

    from bot.notion.client import NotionWriteError

    client = make_client()
    with patch.object(
        client,
        "_write_with_retry",
        new=AsyncMock(side_effect=NotionWriteError("write failed")),
    ):
        with pytest.raises(NotionWriteError):
            await client.create_event("Rave Night 01 Sep 2026", datetime.date(2026, 9, 1))


# ---------------------------------------------------------------------------
# Story 8.3: ensure_dj_page and manual_action
# ---------------------------------------------------------------------------


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://x"))


@pytest.mark.asyncio
async def test_write_with_retry_uses_given_manual_action() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.request = AsyncMock(return_value=_response(400, {}))
    with pytest.raises(NotionWriteError):
        await client._write_with_retry("POST", "/pages", {}, "ctx", manual_action="Do X.")
    assert log.post_error.call_args.kwargs["manual_action"] == "Do X."


@pytest.mark.asyncio
async def test_write_with_retry_default_manual_action_unchanged() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.request = AsyncMock(return_value=_response(400, {}))
    with pytest.raises(NotionWriteError):
        await client._write_with_retry("POST", "/pages", {}, "ctx")
    assert log.post_error.call_args.kwargs["manual_action"] == (
        "Check Notion API key and database permissions. Context: ctx"
    )


@pytest.mark.asyncio
async def test_ensure_dj_page_tds_makes_no_request() -> None:
    log = make_mock_log()
    client = NotionClient(api_key="k", database_id="db", log_service=log, layout="tds")
    client._client.post = AsyncMock()
    client._client.request = AsyncMock()
    assert await client.ensure_dj_page(1, "DJ", "Rave") is None
    client._client.post.assert_not_called()
    client._client.request.assert_not_called()
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_dj_page_returns_existing_page_without_creating() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.post = AsyncMock(return_value=_response(200, {"results": [{"id": "pg-1"}]}))
    client._client.request = AsyncMock()
    assert await client.ensure_dj_page(42, "DJ", "Rave") == "pg-1"
    query = client._client.post.call_args.kwargs["json"]["filter"]
    assert query == {"property": "Discord ID", "rich_text": {"equals": "42"}}
    client._client.request.assert_not_called()  # no POST /pages
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_dj_page_creates_missing_page_with_event_context() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.post = AsyncMock(return_value=_response(200, {"results": []}))
    with patch.object(
        client, "_write_with_retry", new=AsyncMock(return_value={"id": "pg-new"})
    ) as write:
        assert await client.ensure_dj_page(42, "DJ Nova", "Rave Night") == "pg-new"
    kwargs = write.call_args.kwargs
    assert kwargs["endpoint"] == "/pages"
    assert "DJ Nova" in kwargs["context"] and "Rave Night" in kwargs["context"]
    assert "Rave Night" in kwargs["manual_action"]


@pytest.mark.asyncio
async def test_ensure_dj_page_create_failure_posts_one_error_naming_dj_and_event() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.post = AsyncMock(return_value=_response(200, {"results": []}))
    client._client.request = AsyncMock(return_value=_response(400, {}))
    assert await client.ensure_dj_page(42, "DJ Nova", "Rave Night") is None
    log.post_error.assert_awaited_once()
    kwargs = log.post_error.call_args.kwargs
    assert kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE
    assert "DJ Nova" in kwargs["trigger"] and "Rave Night" in kwargs["trigger"]
    assert "add it to the DJs of 'Rave Night'" in kwargs["manual_action"]


@pytest.mark.asyncio
async def test_failed_lookup_still_creates_the_page_without_error() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))  # the query
    client._client.request = AsyncMock(return_value=_response(200, {"id": "pg-new"}))
    assert await client.ensure_dj_page(42, "DJ Nova", "Rave Night") == "pg-new"
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_failed_lookup_and_failed_create_post_one_error() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    client._client.request = AsyncMock(return_value=_response(400, {}))
    assert await client.ensure_dj_page(42, "DJ Nova", "Rave Night") is None
    log.post_error.assert_awaited_once()
    kwargs = log.post_error.call_args.kwargs
    assert "DJ Nova" in kwargs["trigger"] and "Rave Night" in kwargs["trigger"]


@pytest.mark.asyncio
async def test_unexpected_answer_posts_one_error() -> None:
    log = make_mock_log()
    client = make_client(log)
    client._client.post = AsyncMock(return_value=_response(200, {"results": []}))
    client._client.request = AsyncMock(return_value=_response(200, {"no": "id"}))
    assert await client.ensure_dj_page(42, "DJ Nova", "Rave Night") is None
    log.post_error.assert_awaited_once()
    assert "Rave Night" in log.post_error.call_args.kwargs["manual_action"]


@pytest.mark.asyncio
async def test_write_dj_record_returns_existing_page_for_resubmitted_profile() -> None:
    """Onboarding again must not create a second DJ page (Story 8.3 review)."""
    log = make_mock_log()
    client = make_client(log)
    client._client.post = AsyncMock(return_value=_response(200, {"results": [{"id": "pg-1"}]}))
    client._client.request = AsyncMock()
    assert await client.write_dj_record(discord_user_id=42, dj_name="DJ") == "pg-1"
    client._client.request.assert_not_called()


@pytest.mark.asyncio
async def test_delete_booking_without_page_writes_nothing_and_logs_nothing() -> None:
    """Classic cancel of a DJ without a Notion page: no request, no error (Story 8.3 AC3)."""
    log = make_mock_log()
    client = make_client(log)
    client._client.request = AsyncMock()
    client._client.get = AsyncMock()
    client._client.post = AsyncMock()
    await client.delete_booking("evt", None, [], "Rave", 0)
    client._client.request.assert_not_called()
    client._client.get.assert_not_called()
    client._client.post.assert_not_called()
    log.post_error.assert_not_called()


# ---------------------------------------------------------------------------
# Story 8.4: property types
# ---------------------------------------------------------------------------


def _schema_client(properties: dict) -> tuple[NotionClient, MagicMock]:
    log = make_mock_log()
    client = make_client(log)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"properties": properties})
    client._client.get = AsyncMock(return_value=response)
    return client, log


@pytest.mark.asyncio
async def test_validate_schema_classic_map_with_correct_types_posts_nothing() -> None:
    from bot.slots import CLASSIC_PROPERTIES

    client, log = _schema_client(
        {name: {"type": types[0]} for name, types in CLASSIC_PROPERTIES.items()}
    )
    await client.validate_schema(CLASSIC_PROPERTIES)
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_validate_schema_wrong_type_names_property_types_and_fix() -> None:
    client, log = _schema_client({"Slots": {"type": "rich_text"}})
    await client.validate_schema({"Slots": ("number",)})
    log.post_error.assert_awaited_once()
    kwargs = log.post_error.call_args.kwargs
    assert kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE
    assert "'Slots'" in kwargs["trigger"]
    assert "'rich_text'" in kwargs["trigger"] and "expected number" in kwargs["trigger"]
    assert 'to "Number"' in kwargs["manual_action"]


@pytest.mark.asyncio
async def test_validate_schema_reports_each_wrong_and_missing_property() -> None:
    client, log = _schema_client({
        "Name": {"type": "rich_text"},   # wrong
        "Date": {"type": "date"},        # ok
        "DJs": {"type": "multi_select"},  # wrong
    })
    await client.validate_schema(
        {"Name": ("title",), "Date": ("date",), "DJs": ("relation",), "Slots": ("number",)}
    )
    triggers = [c.kwargs["trigger"] for c in log.post_error.call_args_list]
    assert len(triggers) == 3
    assert any("'Name'" in t and "rich_text" in t for t in triggers)
    assert any("'DJs'" in t and "multi_select" in t for t in triggers)
    assert any("missing required property: 'Slots'" in t for t in triggers)


@pytest.mark.asyncio
async def test_validate_schema_accepts_any_allowed_type() -> None:
    client, log = _schema_client({"Status": {"type": "select"}})
    await client.validate_schema({"Status": ("status", "select")})
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_validate_schema_uses_the_real_property_name() -> None:
    client, log = _schema_client({"Discord  ID": {"type": "number"}})
    await client.validate_schema({"Discord ID": ("rich_text",)})
    kwargs = log.post_error.call_args.kwargs
    assert "'Discord  ID'" in kwargs["trigger"]
    assert "named 'Discord ID'" in kwargs["manual_action"]


@pytest.mark.asyncio
async def test_validate_schema_relation_to_another_database_is_reported() -> None:
    client, log = _schema_client(
        {"DJs": {"type": "relation", "relation": {"database_id": "other-db-0000"}}}
    )
    await client.validate_schema({"DJs": ("relation",)}, relation_targets={"DJs": "test-db-id"})
    log.post_error.assert_awaited_once()
    kwargs = log.post_error.call_args.kwargs
    assert "'DJs'" in kwargs["trigger"] and "other-db-0000" in kwargs["trigger"]


@pytest.mark.asyncio
async def test_validate_schema_self_relation_with_dashes_is_accepted() -> None:
    client, log = _schema_client(
        {"DJs": {"type": "relation", "relation": {"database_id": "ab12-cd34"}}}
    )
    await client.validate_schema({"DJs": ("relation",)}, relation_targets={"DJs": "AB12CD34"})
    log.post_error.assert_not_called()


def test_relation_targets_per_layout() -> None:
    classic = make_client()
    assert classic.relation_targets() == {"DJs": "test-db-id"}
    tds = NotionClient(api_key="k", database_id="db", log_service=make_mock_log(), layout="tds")
    assert tds.relation_targets() == {}


# ---------------------------------------------------------------------------
# Story 8.5: separate DJ database (NOTION_DJ_DATABASE_ID)
# ---------------------------------------------------------------------------


def _client_with_dj_db(log: MagicMock | None = None, layout: str = "classic") -> NotionClient:
    return NotionClient(api_key="k", database_id="events-db", log_service=log or make_mock_log(),
                        layout=layout, dj_database_id="dj-db")


def _event(page_id: str, date: str | None, discord_id: str = "") -> dict:
    props_ = {"Date": {"type": "date", "date": {"start": date} if date else None}}
    if discord_id is not None:
        props_["Discord ID"] = {
            "type": "rich_text", "rich_text": [{"plain_text": discord_id}] if discord_id else []
        }
    return {"id": page_id, "properties": props_}


@pytest.mark.asyncio
@pytest.mark.parametrize("separate", [False, True])
async def test_dj_pages_are_written_and_found_in_the_dj_database(separate: bool) -> None:
    client = _client_with_dj_db() if separate else make_client()
    dj_db = "dj-db" if separate else "test-db-id"
    client._client.post = AsyncMock(return_value=_response(200, {"results": []}))
    with patch.object(
        client, "_write_with_retry", new=AsyncMock(return_value={"id": "pg"})
    ) as write:
        await client.write_dj_record(discord_user_id=7, dj_name="Nova")
    assert client._client.post.call_args.args[0] == f"/databases/{dj_db}/query"
    assert write.call_args.kwargs["payload"]["parent"] == {"database_id": dj_db}
    assert client.separate_dj_database is separate
    assert client.relation_targets() == {"DJs": dj_db}


@pytest.mark.asyncio
async def test_events_stay_in_the_events_database_with_a_dj_database() -> None:
    import datetime

    client = _client_with_dj_db()
    with patch.object(
        client, "_write_with_retry", new=AsyncMock(return_value={"id": "evt"})
    ) as write:
        await client.create_event("Rave", datetime.date(2026, 6, 14))
    assert write.call_args.kwargs["payload"]["parent"] == {"database_id": "events-db"}


@pytest.mark.asyncio
async def test_poll_events_shared_database_drops_dj_pages() -> None:
    client = make_client()
    pages = [_event("evt", "2026-06-14"), _event("dj", "2026-06-14", "123"),
             _event("undated", None)]
    client._client.post = AsyncMock(
        return_value=_response(200, {"results": pages, "has_more": False})
    )
    assert [p["id"] for p in await client.poll_events()] == ["evt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["classic", "tds"])
async def test_poll_events_without_dj_pages_uses_the_date_filter_only(layout: str) -> None:
    client = (_client_with_dj_db(layout="classic") if layout == "classic" else
              NotionClient(api_key="k", database_id="events-db", log_service=make_mock_log(),
                           layout="tds"))
    pages = [_event("evt", "2026-06-14", "not a DJ marker here"), _event("undated", None)]
    client._client.post = AsyncMock(
        return_value=_response(200, {"results": pages, "has_more": False})
    )
    result = await client.poll_events()
    assert [p["id"] for p in result] == ["evt"]  # undated pages are never events
    payload = client._client.post.call_args.kwargs["json"]
    assert payload["filter"]["property"] == "Date" and "and" not in payload["filter"]


@pytest.mark.asyncio
async def test_validate_dj_schema_checks_the_dj_database() -> None:
    log = make_mock_log()
    client = _client_with_dj_db(log)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"properties": {
        "Name": {"type": "title"}, "Discord ID": {"type": "number"},
    }})
    client._client.get = AsyncMock(return_value=response)
    await client.validate_dj_schema()
    assert client._client.get.call_args.args[0] == "/databases/dj-db"
    log.post_error.assert_awaited_once()
    trigger = log.post_error.call_args.kwargs["trigger"]
    assert "'Discord ID' in the DJ database" in trigger and "'number'" in trigger


@pytest.mark.asyncio
async def test_validate_dj_schema_does_nothing_in_shared_mode_or_tds() -> None:
    for client in (make_client(), _client_with_dj_db(layout="tds")):
        client._client.get = AsyncMock()
        await client.validate_dj_schema()
        client._client.get.assert_not_called()


@pytest.mark.asyncio
async def test_events_database_messages_name_the_events_database() -> None:
    client, log = _schema_client({})
    await client.validate_schema({"Slots": ("number",)})
    assert "Notion events database missing required property: 'Slots'" in (
        log.post_error.call_args.kwargs["trigger"]
    )


@pytest.mark.asyncio
async def test_dj_page_check_needs_no_request_in_shared_mode() -> None:
    client = make_client()
    client._client.get = AsyncMock()
    assert await client.dj_page_in_dj_database("pg") is True
    client._client.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "expected"), [
    (_response(200, {"parent": {"type": "database_id", "database_id": "d-j-db"}}), True),
    (_response(200, {"parent": {"type": "database_id", "database_id": "events-db"}}), False),
    (_response(404, {}), False),
    (httpx.ConnectError("down"), True),  # unknown: the write reports a real problem itself
])
async def test_dj_page_check_in_separate_mode(answer, expected) -> None:
    client = _client_with_dj_db()
    client._client.get = AsyncMock(
        side_effect=answer if isinstance(answer, Exception) else None,
        return_value=None if isinstance(answer, Exception) else answer,
    )
    assert await client.dj_page_in_dj_database("pg") is expected
    assert client._client.get.call_args.args[0] == "/pages/pg"
