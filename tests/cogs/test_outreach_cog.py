"""Tests for bot/cogs/outreach_cog.py — loop start-up behaviour."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_outreach_loop_waits_until_ready_before_first_iteration() -> None:
    from bot.cogs.outreach_cog import OutreachCog

    loop = OutreachCog._outreach_check
    assert loop._before_loop is not None

    cog = OutreachCog(MagicMock())
    cog.bot = MagicMock()
    cog.bot.wait_until_ready = AsyncMock()
    await loop._before_loop(cog)

    cog.bot.wait_until_ready.assert_awaited_once()
