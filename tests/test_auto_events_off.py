"""Automatic event creation is off unless an organizer sets a look-ahead (no surprise pages)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.cogs.events import EventsCog


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "0"])
async def test_no_events_created_when_lookahead_unset_or_zero(raw) -> None:
    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.notion_client = AsyncMock()
    cog = EventsCog(ctx, MagicMock())
    with patch("bot.cogs.events.get_value", new=AsyncMock(return_value=raw)):
        await cog._auto_create_events([], guild_id=42)
    ctx.notion_client.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_add_event_reply_uses_configured_start_time() -> None:
    from bot import schedule
    from bot.cogs.admin import AdminCog

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.notion_client = AsyncMock()
    cog = AdminCog(ctx)
    inter = MagicMock()
    inter.guild_id = 42
    inter.response.send_message = AsyncMock()
    berlin = schedule.parse_schedule("Europe/Berlin", "21:00", "60")
    with patch("bot.schedule._current", berlin), patch(
        "bot.cogs.admin.init_event_slots", new=AsyncMock(), create=True
    ), patch("bot.cogs.admin.db_config.get_value", new=AsyncMock(return_value=None)):
        await AdminCog.add_event.callback(cog, inter, date_str="2026-10-24")
    text = inter.response.send_message.call_args[0][0]
    # 21:00 CEST on 24 Oct 2026 = 19:00 UTC
    assert "<t:1792868400:f>" in text, text
