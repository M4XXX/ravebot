"""Tests for bot.services.dj_sync."""
from unittest.mock import AsyncMock, MagicMock, patch

from bot.notion.api import NotionWriteError
from bot.notion.client import DjLogoUploadError
from bot.services.dj_sync import retry_unsynced_logos, sync_dj

ROW = {"dj_name": "DJ A", "vrchat_name": "v", "logo_url": None, "logo_image": b"img",
       "stream_url_primary": "https://t.tv/a", "stream_url_backup": None,
       "logo_synced": False}


def _ctx(upsert=None):
    ctx = MagicMock()
    ctx.notion_client.upsert_dj = upsert or AsyncMock(return_value="page-1")
    return ctx


def _p(row=ROW):
    return (
        patch("bot.services.dj_sync.get_dj_by_discord_id", new=AsyncMock(return_value=row)),
        patch("bot.services.dj_sync.set_notion_dj_page_id", new=AsyncMock()),
        patch("bot.services.dj_sync.set_logo_synced", new=AsyncMock()),
    )


async def test_success_marks_synced_and_stores_page():
    ctx = _ctx()
    g, s, lg = _p()
    with g, s as setp, lg as setl:
        assert await sync_dj(ctx, 1, "alice") is True
    assert ctx.notion_client.upsert_dj.await_args.kwargs["logo_image"] == b"img"
    setp.assert_awaited_once_with(ctx.db, 1, "page-1")
    setl.assert_awaited_once_with(ctx.db, 1, True)


async def test_success_without_logo_also_marks_synced():
    ctx = _ctx()
    g, s, lg = _p({**ROW, "logo_image": None})
    with g, s, lg as setl:
        assert await sync_dj(ctx, 1, "alice") is True
    setl.assert_awaited_once_with(ctx.db, 1, True)


async def test_failure_returns_false_and_leaves_unsynced():
    ctx = _ctx(AsyncMock(side_effect=NotionWriteError("down")))
    g, s, lg = _p()
    with g, s as setp, lg as setl:
        assert await sync_dj(ctx, 1, "alice") is False
    setp.assert_not_called()
    setl.assert_awaited_once_with(ctx.db, 1, False)


async def test_failure_without_logo_does_not_touch_flag():
    ctx = _ctx(AsyncMock(side_effect=NotionWriteError("down")))
    g, s, lg = _p({**ROW, "logo_image": None})
    with g, s, lg as setl:
        assert await sync_dj(ctx, 1, "alice") is False
    setl.assert_not_called()


async def test_missing_row_returns_false():
    ctx = _ctx()
    g, s, lg = _p(None)
    with g, s, lg:
        assert await sync_dj(ctx, 1, "alice") is False


async def test_retry_counts_successes_and_continues():
    ctx = _ctx()
    member = MagicMock(display_name="Guild A")
    guild = MagicMock()
    guild.get_member = lambda uid: member if uid == 1 else None
    calls = []

    async def fake_sync(c, uid, name):
        calls.append((uid, name))
        if uid == 2:
            raise RuntimeError("boom")
        return uid != 3

    with patch("bot.services.dj_sync.list_unsynced_logo_user_ids",
               new=AsyncMock(return_value=[1, 2, 3, 4])), \
         patch("bot.services.dj_sync.sync_dj", new=fake_sync), \
         patch("bot.services.dj_sync.get_dj_by_discord_id",
               new=AsyncMock(return_value={"dj_name": "Stored"})):
        assert await retry_unsynced_logos(ctx, guild) == 2
    assert calls[0] == (1, "Guild A")
    assert [c[0] for c in calls] == [1, 2, 3, 4]
    assert calls[3][1] == "Stored"


async def test_retry_never_raises_on_list_failure():
    ctx = _ctx()
    with patch("bot.services.dj_sync.list_unsynced_logo_user_ids",
               new=AsyncMock(side_effect=RuntimeError("db"))):
        assert await retry_unsynced_logos(ctx, None) == 0


async def test_unsynced_logo_is_uploaded():
    ctx = _ctx()
    g, s, lg = _p()
    with g, s, lg:
        assert await sync_dj(ctx, 1, "alice") is True
    assert ctx.notion_client.upsert_dj.await_args.kwargs["upload_logo"] is True


async def test_synced_logo_is_not_uploaded_again():
    ctx = _ctx()
    g, s, lg = _p({**ROW, "logo_synced": True})
    with g, s, lg as setl:
        assert await sync_dj(ctx, 1, "alice") is True
    assert ctx.notion_client.upsert_dj.await_args.kwargs["upload_logo"] is False
    setl.assert_awaited_once_with(ctx.db, 1, True)


async def test_logo_upload_failure_keeps_page_and_leaves_logo_unsynced():
    ctx = _ctx(AsyncMock(side_effect=DjLogoUploadError("page-7")))
    g, s, lg = _p()
    with g, s as setp, lg as setl:
        assert await sync_dj(ctx, 1, "alice") is False
    setp.assert_awaited_once_with(ctx.db, 1, "page-7")
    setl.assert_awaited_once_with(ctx.db, 1, False)
