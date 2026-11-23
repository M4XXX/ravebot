"""Tests for bot/db/dm_menus.py."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.dm_menus import LastMenu, get_menu, set_menu


@pytest.mark.asyncio
async def test_get_menu_none_when_missing() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await get_menu(pool, 5) is None
    assert pool.fetchrow.await_args.args[1] == 5


@pytest.mark.asyncio
async def test_get_menu_returns_record_with_ints() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"user_id": 5, "channel_id": 7, "message_id": 99})
    assert await get_menu(pool, 5) == LastMenu(5, 7, 99)


@pytest.mark.asyncio
async def test_set_menu_upserts_on_user_id() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    await set_menu(pool, 5, 7, 99)
    sql, *args = pool.execute.await_args.args
    assert "INSERT INTO dm_menus" in sql
    assert "ON CONFLICT (user_id) DO UPDATE" in sql
    assert args == [5, 7, 99]
