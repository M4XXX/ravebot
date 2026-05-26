"""Unit tests for AdminLogService.post_dm_transcript().

[Source: story 6-1-dm-transcript-logging-to-admin-channel.md#Tasks 7]
"""
import logging
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.log import AdminLogService


def _make_service(channel=None):
    bot = MagicMock()
    db = MagicMock()
    svc = AdminLogService(bot=bot, db=db)
    svc._get_log_channel = AsyncMock(return_value=channel)
    return svc


@pytest.mark.asyncio
async def test_post_dm_transcript_bot_direction_posts_to_channel():
    """direction='BOT' → '[BOT → @dj] msg' sent to channel."""
    channel = MagicMock()
    channel.send = AsyncMock()
    svc = _make_service(channel=channel)

    await svc.post_dm_transcript(dj_name="DJ Test", direction="BOT", content="Hello!")

    channel.send.assert_awaited_once()
    text = channel.send.call_args[0][0]
    assert "[BOT" in text
    assert "@DJ Test" in text
    assert "Hello!" in text


@pytest.mark.asyncio
async def test_post_dm_transcript_dj_direction_posts_to_channel():
    """direction='DJ' → '[@dj → BOT] msg' sent to channel."""
    channel = MagicMock()
    channel.send = AsyncMock()
    svc = _make_service(channel=channel)

    await svc.post_dm_transcript(dj_name="DJ Test", direction="DJ", content="Hey bot")

    channel.send.assert_awaited_once()
    text = channel.send.call_args[0][0]
    assert "@DJ Test" in text
    assert "BOT" in text
    assert "Hey bot" in text


@pytest.mark.asyncio
async def test_post_dm_transcript_button_format():
    """Button tap content preserved in transcript entry."""
    channel = MagicMock()
    channel.send = AsyncMock()
    svc = _make_service(channel=channel)

    await svc.post_dm_transcript(
        dj_name="Spinmaster", direction="DJ", content="[Button: \u2705 Go Active]"
    )

    text = channel.send.call_args[0][0]
    assert "[Button: \u2705 Go Active]" in text
    assert "@Spinmaster" in text


@pytest.mark.asyncio
async def test_post_dm_transcript_no_channel_does_not_raise(caplog):
    """No log channel → no crash, stdout warning emitted."""
    svc = _make_service(channel=None)

    with caplog.at_level(logging.WARNING, logger="bot.log"):
        await svc.post_dm_transcript(dj_name="DJ X", direction="BOT", content="msg")

    assert len(caplog.records) > 0
    assert any("no log channel" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_post_dm_transcript_discord_exception_does_not_raise():
    """channel.send raises DiscordException → no crash."""
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=disnake.DiscordException())
    svc = _make_service(channel=channel)

    # Must not raise
    await svc.post_dm_transcript(dj_name="DJ Y", direction="DJ", content="test")
