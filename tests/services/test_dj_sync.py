"""Tests for bot.services.dj_sync: DJ changes are queued for the Notion mirror."""
from unittest.mock import AsyncMock, MagicMock, call, patch

from bot.services.dj_sync import queue_unsynced_logos, sync_dj


async def test_sync_dj_queues_the_dj_and_never_calls_notion():
    ctx = MagicMock()
    with patch("bot.services.dj_sync.changed") as changed:
        assert await sync_dj(ctx, 42) is True
    changed.assert_called_once_with(ctx, "dj", "42")
    assert ctx.notion_client.method_calls == []
    assert ctx.db.method_calls == []


async def test_sync_dj_works_without_notion():
    ctx = MagicMock(notion_client=None, mirror=None)
    assert await sync_dj(ctx, -3) is True


async def test_unsynced_logos_are_queued_for_the_worker():
    ctx = MagicMock()
    with patch("bot.services.dj_sync.list_unsynced_logo_user_ids",
               new=AsyncMock(return_value=[1, -2])) as listed, \
         patch("bot.services.dj_sync.changed") as changed:
        assert await queue_unsynced_logos(ctx) == 2
    listed.assert_awaited_once_with(ctx.db)
    assert changed.call_args_list == [call(ctx, "dj", "1"), call(ctx, "dj", "-2")]
    assert ctx.notion_client.method_calls == []


async def test_unsynced_logos_not_queued_without_the_mirror():
    ctx = MagicMock(mirror=None)
    with patch("bot.services.dj_sync.list_unsynced_logo_user_ids", new=AsyncMock()) as listed:
        assert await queue_unsynced_logos(ctx) == 0
    listed.assert_not_awaited()


async def test_unsynced_logos_list_failure_never_raises():
    ctx = MagicMock()
    with patch("bot.services.dj_sync.list_unsynced_logo_user_ids",
               new=AsyncMock(side_effect=RuntimeError("db"))), \
         patch("bot.services.dj_sync.changed") as changed:
        assert await queue_unsynced_logos(ctx) == 0
    changed.assert_not_called()
