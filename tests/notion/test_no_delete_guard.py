"""Tests for the no-delete guard in bot/notion/client.py — the bot never deletes Notion content."""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bot.notion.client import NotionClient, NotionDeletionBlockedError, _refuse_deletion


def make_guarded_client(sent: list[httpx.Request]) -> httpx.AsyncClient:
    """AsyncClient with the guard hook and a transport that records what actually got sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"object": "page", "id": "page-1"})

    return httpx.AsyncClient(
        base_url="https://api.notion.com/v1",
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [_refuse_deletion]},
    )


@pytest.mark.asyncio
async def test_delete_method_is_blocked() -> None:
    sent: list[httpx.Request] = []
    async with make_guarded_client(sent) as client:
        with pytest.raises(NotionDeletionBlockedError):
            await client.request("DELETE", "/blocks/page-1")
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["archived", "in_trash", "is_archived"])
async def test_trash_flag_is_blocked(key: str) -> None:
    sent: list[httpx.Request] = []
    async with make_guarded_client(sent) as client:
        with pytest.raises(NotionDeletionBlockedError):
            await client.request("PATCH", "/pages/page-1", json={key: True})
    assert sent == []


@pytest.mark.asyncio
async def test_nested_trash_flag_is_blocked() -> None:
    sent: list[httpx.Request] = []
    async with make_guarded_client(sent) as client:
        with pytest.raises(NotionDeletionBlockedError):
            await client.request(
                "PATCH", "/blocks/page-1/children", json={"children": [{"archived": True}]}
            )
    assert sent == []


@pytest.mark.asyncio
async def test_normal_writes_pass() -> None:
    sent: list[httpx.Request] = []
    async with make_guarded_client(sent) as client:
        await client.request("POST", "/pages", json={"properties": {"Name": {"title": []}}})
        await client.request("PATCH", "/pages/page-1", json={"archived": False})
        await client.request("GET", "/databases/db-1")
    assert [r.method for r in sent] == ["POST", "PATCH", "GET"]


def test_notion_client_registers_guard() -> None:
    client = NotionClient(api_key="secret_test", database_id="db", log_service=MagicMock())
    assert _refuse_deletion in client._client.event_hooks["request"]


@pytest.mark.asyncio
async def test_write_with_retry_does_not_retry_blocked_delete() -> None:
    log = MagicMock()
    log.post_error = AsyncMock(return_value=None)
    client = NotionClient(api_key="secret_test", database_id="db", log_service=log)
    sent: list[httpx.Request] = []
    client._client = make_guarded_client(sent)
    with pytest.raises(NotionDeletionBlockedError):
        await client._write_with_retry("PATCH", "/pages/page-1", {"in_trash": True}, "test")
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/databases/db-1", {"properties": {"DJs": None}}),          # drops a whole column
    ("/databases/db-1", {"title": []}),                          # blanks the database name
    ("/pages/page-1", {"properties": {"Name": {"title": []}}}),  # blanks an event name
])
async def test_wiping_a_column_or_a_title_is_blocked(path: str, body: dict) -> None:
    sent: list[httpx.Request] = []
    async with make_guarded_client(sent) as client:
        with pytest.raises(NotionDeletionBlockedError):
            await client.request("PATCH", path, json=body)
    assert sent == []


@pytest.mark.asyncio
async def test_unlinking_the_last_dj_is_allowed() -> None:
    """An empty relation is how the last DJ of an event is unlinked."""
    sent: list[httpx.Request] = []
    async with make_guarded_client(sent) as client:
        await client.request("PATCH", "/pages/evt", json={"properties": {"DJs": {"relation": []}}})
    assert len(sent) == 1
