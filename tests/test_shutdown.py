"""docker stop: queued DM transcripts are posted before the bot disconnects."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import main


@pytest.mark.asyncio
async def test_transcripts_are_flushed_before_the_connection_closes() -> None:
    order: list[str] = []
    ctx = MagicMock()
    ctx.log_service.flush_transcripts = AsyncMock(side_effect=lambda: order.append("flush"))
    bot = MagicMock()
    bot.close = AsyncMock(side_effect=lambda: order.append("close"))
    await main.graceful_shutdown(bot, ctx)
    assert order == ["flush", "close"]


@pytest.mark.asyncio
async def test_a_stuck_log_channel_does_not_block_shutdown() -> None:
    async def never_done() -> None:
        await asyncio.sleep(3600)

    ctx = MagicMock()
    ctx.log_service.flush_transcripts = never_done
    bot = MagicMock()
    bot.close = AsyncMock()
    with patch.object(main, "TRANSCRIPT_FLUSH_SECONDS", 0.01):
        await main.graceful_shutdown(bot, ctx)
    bot.close.assert_awaited_once()
