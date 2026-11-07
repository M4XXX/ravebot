"""Tests for bot/db/dj_conversations.py."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


def _row(data):
    return {
        "discord_user_id": 1, "guild_id": 2, "kind": "onboarding", "step": "name",
        "data": data, "logo_image": b"x", "message_id": 7,
    }


@pytest.mark.asyncio
async def test_get_conversation_none_when_missing() -> None:
    from bot.db.dj_conversations import get_conversation

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await get_conversation(pool, 1) is None


@pytest.mark.asyncio
async def test_get_conversation_parses_json_string_and_dict() -> None:
    from bot.db.dj_conversations import Conversation, get_conversation

    for raw in ('{"dj_name": "A"}', {"dj_name": "A"}):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=_row(raw))
        assert await get_conversation(pool, 1) == Conversation(
            1, 2, "onboarding", "name", {"dj_name": "A"}, b"x", 7
        )
    sql = pool.fetchrow.await_args.args[0]
    assert "dj_conversations" in sql and "discord_user_id = $1" in sql


@pytest.mark.asyncio
async def test_save_conversation_upserts() -> None:
    from bot.db.dj_conversations import Conversation, save_conversation

    pool = MagicMock()
    pool.execute = AsyncMock()
    conv = Conversation(1, 2, "edit", "menu", {"a": 1}, None, None)
    await save_conversation(pool, conv)
    sql, *args = pool.execute.await_args.args
    assert "ON CONFLICT (discord_user_id) DO UPDATE" in sql
    assert "updated_at" in sql
    assert args == [1, 2, "edit", "menu", json.dumps({"a": 1}), None, None]


@pytest.mark.asyncio
async def test_delete_and_set_message() -> None:
    from bot.db.dj_conversations import delete_conversation, set_conversation_message

    pool = MagicMock()
    pool.execute = AsyncMock()
    await delete_conversation(pool, 1)
    sql, *args = pool.execute.await_args.args
    assert "DELETE FROM dj_conversations" in sql and args == [1]
    await set_conversation_message(pool, 1, 55)
    sql, *args = pool.execute.await_args.args
    assert "UPDATE dj_conversations" in sql and "message_id" in sql and args == [55, 1]
