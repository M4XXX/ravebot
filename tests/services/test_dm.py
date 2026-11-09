"""Tests for the shared DM helper dm_user."""
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.services.dm import dm_user


def _bot(user=None, fetched=None):
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=fetched)
    return bot


def _user(side_effect=None):
    user = MagicMock()
    user.send = AsyncMock(side_effect=side_effect)
    return user


@pytest.mark.asyncio
async def test_dm_user_sends_and_returns_true():
    user = _user()
    assert await dm_user(_bot(user), 5, "hi") is True
    user.send.assert_awaited_once()
    assert user.send.await_args.args == ("hi",)
    mentions = user.send.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False


@pytest.mark.asyncio
async def test_dm_user_fetches_uncached_user():
    user = _user()
    bot = _bot(None, user)
    assert await dm_user(bot, 5, "hi") is True
    bot.fetch_user.assert_awaited_once_with(5)
    assert user.send.await_args.args == ("hi",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        disnake.NotFound(MagicMock(status=404, reason="Not Found"), "Not Found"),
        disnake.Forbidden(MagicMock(status=403, reason="Forbidden"), "Forbidden"),
        RuntimeError("boom"),
    ],
)
async def test_dm_user_failure_returns_false(exc):
    assert await dm_user(_bot(_user(exc)), 5, "hi") is False


@pytest.mark.asyncio
async def test_dm_user_fetch_failure_returns_false():
    bot = _bot(None)
    bot.fetch_user = AsyncMock(
        side_effect=disnake.NotFound(MagicMock(status=404, reason="Not Found"), "Not Found")
    )
    assert await dm_user(bot, 5, "hi") is False


@pytest.mark.asyncio
async def test_dm_user_guest_has_nothing_to_send():
    bot = _bot()
    assert await dm_user(bot, -2, "hi") is True
    bot.get_user.assert_not_called()
    bot.fetch_user.assert_not_awaited()
