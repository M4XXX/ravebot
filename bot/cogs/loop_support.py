"""Shared helpers for the background task loops (EventsCog, OutreachCog).

A disnake tasks.loop stops for good when its body raises anything other than a few
network errors. run_cycle catches everything, so one bad cycle (a database hiccup,
an unexpected reply) only skips that cycle.
"""
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from bot.timezone import load_stored_timezone_for_bot
from bot.ui.embeds import ErrorCode

logger = logging.getLogger(__name__)


async def run_cycle(name: str, body: Callable[[], Awaitable[None]]) -> None:
    """Run one loop cycle; any exception is logged and the loop goes on."""
    try:
        await body()
    except Exception:
        logger.exception("%s: the cycle failed; the next cycle tries again", name)


async def guard_db(ctx: Any, operation: str) -> bool:
    """True if the database answers; otherwise post ERR_DB_UNAVAILABLE and return False.

    Callers skip the cycle on False; the loop retries on the next one.
    """
    if ctx.db is None:
        logger.warning("DB pool not available during %s — skipping cycle", operation)
        await _post_unavailable(ctx, "The bot cannot reach its database.")
        return False
    try:
        await ctx.db.fetchval("SELECT 1")
        return True
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as exc:
        logger.warning("DB unavailable during %s: %s", operation, exc)
        await _post_unavailable(ctx, "The bot lost its connection to the database.")
        return False


async def _post_unavailable(ctx: Any, trigger: str) -> None:
    if ctx.log_service is None:
        return
    try:
        await ctx.log_service.post_error(
            error_code=ErrorCode.ERR_DB_UNAVAILABLE,
            trigger=trigger,
            action_taken="It skips this run and tries again on the next one.",
        )
    except Exception:
        logger.warning("Could not post ERR_DB_UNAVAILABLE", exc_info=True)


def first_guild_id(bot: Any) -> int | None:
    """The id of the server the bot works on, or None before it joined one."""
    if not bot.guilds:
        logger.warning("The bot has no guilds yet — skipping cycle")
        return None
    return bot.guilds[0].id


async def prepare_loop(bot: Any, db: Any) -> None:
    """before_loop: wait for Discord, then load the stored time zone (best effort)."""
    await bot.wait_until_ready()
    try:
        await load_stored_timezone_for_bot(bot, db)
    except Exception:
        logger.warning("Loading the stored time zone failed; using the default", exc_info=True)
