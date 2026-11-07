"""Tests for bot/services/activation.py (real functions, no activation patch)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services import activation


def _db(values: dict[str, str]):
    async def get(db, guild_id, key):
        return values.get(key)

    return get


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"bot_active": "true"}, True),
        ({"bot_active": "true", "dj_channel": "1"}, True),
        ({"bot_active": "false", "dj_channel": "1", "log_channel": "2"}, False),
        ({"dj_channel": "1", "log_channel": "2"}, True),
        ({"dj_channel": "1"}, False),
        ({"log_channel": "2"}, False),
        ({}, False),
    ],
)
async def test_is_active(monkeypatch, values, expected) -> None:
    monkeypatch.setattr(activation, "get_value", _db(values))
    assert await activation.is_active(MagicMock(), 5) is expected


@pytest.mark.asyncio
async def test_set_active_stores_true(monkeypatch) -> None:
    setter = AsyncMock()
    monkeypatch.setattr(activation, "set_value", setter)
    db = MagicMock()
    await activation.set_active(db, 5)
    setter.assert_awaited_once_with(db, 5, "bot_active", "true")


@pytest.mark.asyncio
async def test_bot_is_active_uses_first_guild(monkeypatch) -> None:
    seen = AsyncMock(return_value=True)
    monkeypatch.setattr(activation, "is_active", seen)
    bot = MagicMock(guilds=[MagicMock(id=7), MagicMock(id=8)])
    db = MagicMock()
    assert await activation.bot_is_active(bot, db) is True
    seen.assert_awaited_once_with(db, 7)


@pytest.mark.asyncio
async def test_bot_is_active_false_without_guild_or_db(monkeypatch) -> None:
    seen = AsyncMock(return_value=True)
    monkeypatch.setattr(activation, "is_active", seen)
    assert await activation.bot_is_active(MagicMock(guilds=[]), MagicMock()) is False
    assert await activation.bot_is_active(MagicMock(guilds=[MagicMock(id=1)]), None) is False
    seen.assert_not_awaited()
