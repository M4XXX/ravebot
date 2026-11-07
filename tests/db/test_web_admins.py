"""Tests for bot/db/web_admins.py."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_list_web_admins_orders_by_added_at_then_id() -> None:
    from bot.db.web_admins import list_web_admins

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"discord_user_id": 5}, {"discord_user_id": 3}])
    assert await list_web_admins(pool) == [5, 3]
    sql = pool.fetch.await_args.args[0]
    assert "FROM web_admins" in sql and "ORDER BY added_at, discord_user_id" in sql


@pytest.mark.asyncio
async def test_add_web_admin_true_only_when_inserted() -> None:
    from bot.db.web_admins import add_web_admin

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    assert await add_web_admin(pool, 7) is True
    sql, user_id = pool.execute.await_args.args
    assert "INSERT INTO web_admins" in sql and "ON CONFLICT" in sql and "DO NOTHING" in sql
    assert user_id == 7
    pool.execute = AsyncMock(return_value="INSERT 0 0")
    assert await add_web_admin(pool, 7) is False


@pytest.mark.asyncio
async def test_remove_web_admin_true_only_when_deleted() -> None:
    from bot.db.web_admins import remove_web_admin

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 1")
    assert await remove_web_admin(pool, 7) is True
    sql, user_id = pool.execute.await_args.args
    assert "DELETE FROM web_admins" in sql and user_id == 7
    pool.execute = AsyncMock(return_value="DELETE 0")
    assert await remove_web_admin(pool, 7) is False
