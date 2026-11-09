"""Background loops survive unexpected errors (review finding 13)."""
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg

from bot.cogs.loop_support import guard_db, run_cycle


async def test_run_cycle_swallows_any_error() -> None:
    body = AsyncMock(side_effect=asyncpg.InterfaceError("pool closed"))
    await run_cycle("test", body)  # does not raise
    body.assert_awaited_once()


async def test_guard_db_treats_any_postgres_error_as_unavailable() -> None:
    ctx = MagicMock()
    ctx.db.fetchval = AsyncMock(side_effect=asyncpg.TooManyConnectionsError("full"))
    ctx.log_service.post_error = AsyncMock(side_effect=RuntimeError("discord down"))
    assert await guard_db(ctx, "op") is False


async def test_poll_and_outreach_loops_survive_an_activation_error() -> None:
    from bot.cogs.events import EventsCog
    from bot.cogs.outreach_cog import OutreachCog

    ctx = MagicMock()
    ctx.db.fetchval = AsyncMock(return_value=1)
    for cog_cls, loop in ((EventsCog, "_poll_notion"), (EventsCog, "_check_reminders"),
                          (OutreachCog, "_outreach_check")):
        cog = cog_cls(ctx)
        cog.bot = MagicMock(guilds=[MagicMock(id=1)])
        with patch(
            "bot.services.activation.is_active",
            new=AsyncMock(side_effect=asyncpg.InterfaceError("x")),
        ):
            await getattr(cog, loop).coro(cog)  # must not raise
