"""Tests for bot.services.dj_pages.ensure_dj_page."""
from unittest.mock import AsyncMock, MagicMock, patch

from bot.notion.api import NotionWriteError
from bot.services.dj_pages import ensure_dj_page

ROW = {"dj_name": "DJ A", "vrchat_name": "vrc", "logo_url": "https://l.test/a.png",
       "stream_url_primary": "https://twitch.tv/a", "stream_url_backup": "https://alt.test"}


def _patch(stored, row):
    return (
        patch("bot.services.dj_pages.get_notion_dj_page_id", new=AsyncMock(return_value=stored)),
        patch("bot.services.dj_pages.get_dj_by_discord_id", new=AsyncMock(return_value=row)),
        patch("bot.services.dj_pages.set_notion_dj_page_id", new=AsyncMock()),
    )


def _notion(alive=True):
    notion = MagicMock()
    notion.upsert_dj = AsyncMock(return_value="new-page")
    notion.dj_page_alive = AsyncMock(return_value=alive)
    notion.refresh_discord_name = AsyncMock()
    return notion


async def test_returns_alive_stored_id_and_refreshes_discord_name():
    notion = _notion(alive=True)
    p1, p2, p3 = _patch("stored", ROW)
    with p1, p2 as get_row, p3 as setter:
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") == "stored"
    notion.dj_page_alive.assert_awaited_once_with("stored")
    notion.refresh_discord_name.assert_awaited_once_with("stored", "alice")
    notion.upsert_dj.assert_not_called()
    get_row.assert_not_called()
    setter.assert_not_called()


async def test_refresh_failure_does_not_block():
    notion = _notion(alive=True)
    notion.refresh_discord_name.side_effect = NotionWriteError("down")
    p1, p2, p3 = _patch("stored", ROW)
    with p1, p2, p3:
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") == "stored"


async def test_dead_stored_id_is_replaced_by_fresh_upsert():
    notion = _notion(alive=False)
    db = MagicMock()
    p1, p2, p3 = _patch("stale", ROW)
    with p1, p2, p3 as setter:
        assert await ensure_dj_page(db, notion, 1, "alice") == "new-page"
    notion.dj_page_alive.assert_awaited_once_with("stale")
    notion.upsert_dj.assert_awaited_once()
    notion.refresh_discord_name.assert_not_called()
    setter.assert_awaited_once_with(db, 1, "new-page")


async def test_alive_check_error_returns_none():
    notion = _notion()
    notion.dj_page_alive.side_effect = NotionWriteError("500", 500)
    p1, p2, p3 = _patch("stored", ROW)
    with p1, p2, p3 as setter:
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") is None
    notion.upsert_dj.assert_not_called()
    setter.assert_not_called()


async def test_any_exception_returns_none():
    notion = _notion(alive=False)
    notion.upsert_dj.side_effect = RuntimeError("bug")
    p1, p2, p3 = _patch("stale", ROW)
    with p1, p2, p3:
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") is None


async def test_db_exception_returns_none():
    notion = _notion()
    with patch("bot.services.dj_pages.get_notion_dj_page_id",
               new=AsyncMock(side_effect=OSError("db down"))):
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") is None


async def test_creates_and_stores_when_absent():
    notion = MagicMock()
    notion.upsert_dj = AsyncMock(return_value="new-page")
    db = MagicMock()
    p1, p2, p3 = _patch(None, ROW)
    with p1, p2, p3 as setter:
        assert await ensure_dj_page(db, notion, 1, "alice") == "new-page"
    notion.upsert_dj.assert_awaited_once_with(
        1, "DJ A", "alice", vrchat_name="vrc", logo_url="https://l.test/a.png",
        link="https://twitch.tv/a", alt_link="https://alt.test")
    setter.assert_awaited_once_with(db, 1, "new-page")


async def test_empty_stored_id_is_treated_as_absent():
    notion = MagicMock()
    notion.upsert_dj = AsyncMock(return_value="new-page")
    p1, p2, p3 = _patch("", ROW)
    with p1, p2, p3:
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") == "new-page"


async def test_returns_none_when_notion_fails():
    notion = MagicMock()
    notion.upsert_dj = AsyncMock(side_effect=NotionWriteError("down"))
    p1, p2, p3 = _patch(None, ROW)
    with p1, p2, p3 as setter:
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") is None
    setter.assert_not_called()


async def test_returns_none_when_dj_has_no_row():
    notion = MagicMock()
    notion.upsert_dj = AsyncMock()
    p1, p2, p3 = _patch(None, None)
    with p1, p2, p3:
        assert await ensure_dj_page(MagicMock(), notion, 1, "alice") is None
    notion.upsert_dj.assert_not_called()


async def test_empty_name_skips_refresh_for_alive_page():
    notion = _notion(alive=True)
    p1, p2, p3 = _patch("stored", ROW)
    with p1, p2, p3:
        assert await ensure_dj_page(MagicMock(), notion, 1, "") == "stored"
    notion.refresh_discord_name.assert_not_called()


async def test_empty_name_on_create_never_uses_digits_for_discord_name():
    notion = MagicMock()
    notion.upsert_dj = AsyncMock(return_value="new-page")
    p1, p2, p3 = _patch(None, ROW)
    with p1, p2, p3:
        assert await ensure_dj_page(MagicMock(), notion, 1, "") == "new-page"
    args = notion.upsert_dj.await_args.args
    assert args[1] == "DJ A"
    assert args[2] == ""
