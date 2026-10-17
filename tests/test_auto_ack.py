"""Slow handlers are acknowledged within Discord's 3 seconds; fast ones are unchanged."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.ui import ack


def _inter() -> MagicMock:
    state = {"done": False}
    inter = MagicMock()
    inter.response.is_done = MagicMock(side_effect=lambda: state["done"])

    async def defer(**kwargs):
        state["done"] = True

    inter.response.defer = AsyncMock(side_effect=defer)
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_slow_handler_is_deferred_and_answered_by_followup() -> None:
    inter = _inter()
    with patch.object(ack, "ACK_AFTER_SECONDS", 0.01):
        async with ack.AutoAck(inter, ephemeral=True):
            await asyncio.sleep(0.05)  # e.g. a slow Notion PATCH
            await ack.reply(inter, "Slot secured")
    inter.response.defer.assert_awaited_once_with(with_message=True, ephemeral=True)
    inter.followup.send.assert_awaited_once_with("Slot secured")
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_fast_handler_answers_directly() -> None:
    inter = _inter()
    async with ack.AutoAck(inter):
        await ack.reply(inter, "quick")
    await asyncio.sleep(0)  # the cancelled timer must not defer afterwards
    inter.response.send_message.assert_awaited_once_with("quick")
    inter.response.defer.assert_not_called()


@pytest.mark.asyncio
async def test_reply_outside_autoack_is_plain_send_message() -> None:
    inter = _inter()
    await ack.reply(inter, "hello", ephemeral=True)
    inter.response.send_message.assert_awaited_once_with("hello", ephemeral=True)


@pytest.mark.asyncio
async def test_decorator_wraps_only_real_interactions() -> None:
    seen = []

    class Recorder:
        def __init__(self, inter, ephemeral=False):
            seen.append(inter)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    @ack.auto_ack()
    async def handler(self, item, inter):
        return "ran"

    real = object.__new__(disnake.MessageInteraction)
    with patch.object(ack, "AutoAck", Recorder):
        assert await handler(None, MagicMock(), real) == "ran"
        assert await handler(None, MagicMock(), MagicMock()) == "ran"
    assert seen == [real]


@pytest.mark.asyncio
async def test_reply_after_autoack_ended_uses_followup_when_already_deferred() -> None:
    """An error handler runs after the AutoAck block; the 'thinking…' must still be answered."""
    inter = _inter()
    await inter.response.defer()
    await ack.reply(inter, "Something went wrong", ephemeral=True)
    inter.followup.send.assert_awaited_once_with("Something went wrong", ephemeral=True)
    inter.response.send_message.assert_not_called()
