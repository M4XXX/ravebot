"""Tests for bot/db/log_replies.py and migration 0010."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_save_log_reply_inserts_mapping() -> None:
    from bot.db.log_replies import save_log_reply

    pool = MagicMock()
    pool.execute = AsyncMock()
    await save_log_reply(pool, 10, 20)
    sql, *args = pool.execute.await_args.args
    assert "INSERT INTO log_replies" in sql
    assert "ON CONFLICT (message_id) DO UPDATE" in sql
    assert args == [10, 20]


@pytest.mark.asyncio
async def test_get_log_reply_user_returns_user_id() -> None:
    from bot.db.log_replies import get_log_reply_user

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"user_id": 20})
    assert await get_log_reply_user(pool, 10) == 20
    sql, mid = pool.fetchrow.await_args.args
    assert "FROM log_replies" in sql and "message_id = $1" in sql
    assert mid == 10


@pytest.mark.asyncio
async def test_get_log_reply_user_missing_returns_none() -> None:
    from bot.db.log_replies import get_log_reply_user

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await get_log_reply_user(pool, 10) is None


def test_migration_0010_creates_and_drops_log_replies() -> None:
    path = Path(__file__).parents[2] / "migrations/versions/0010_log_replies.py"
    spec = importlib.util.spec_from_file_location("mig0010", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0010", "0009")
    with patch.object(mod, "op") as op:
        mod.upgrade()
    args = op.create_table.call_args.args
    assert args[0] == "log_replies"
    columns = {c.name: c for c in args[1:] if hasattr(c, "nullable")}
    assert set(columns) == {"message_id", "user_id", "created_at"}
    assert columns["created_at"].server_default is not None
    with patch.object(mod, "op") as op:
        mod.downgrade()
    op.drop_table.assert_called_once_with("log_replies")
