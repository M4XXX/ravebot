"""Tests for bot/notion/dj_pages.py — find or create a DJ's Notion page and store its ID.

[Source: story 8-3-handle-djs-without-a-notion-page-id.md]
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.notion.dj_pages import ensure_and_store


def _client(returns):
    client = MagicMock()
    client.ensure_dj_page = AsyncMock(return_value=returns)
    return client


@pytest.mark.asyncio
async def test_found_page_is_stored_and_returned() -> None:
    client, db = _client("pg-1"), MagicMock()
    with patch("bot.notion.dj_pages.set_notion_dj_page_id", new=AsyncMock()) as store:
        assert await ensure_and_store(client, db, 42, "Nova", "Rave") == "pg-1"
    client.ensure_dj_page.assert_awaited_once_with(42, "Nova", "Rave")
    store.assert_awaited_once_with(db, 42, "pg-1")


@pytest.mark.asyncio
async def test_no_page_stores_nothing() -> None:
    with patch("bot.notion.dj_pages.set_notion_dj_page_id", new=AsyncMock()) as store:
        assert await ensure_and_store(_client(None), MagicMock(), 42, "Nova", "Rave") is None
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_store_still_returns_the_page() -> None:
    failing = AsyncMock(side_effect=RuntimeError("db down"))
    with patch("bot.notion.dj_pages.set_notion_dj_page_id", new=failing):
        assert await ensure_and_store(_client("pg-1"), MagicMock(), 42, "Nova", "Rave") == "pg-1"
