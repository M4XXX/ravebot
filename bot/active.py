"""Passive/active switch — a fresh install joins a server without contacting anyone.

While passive (the default) the bot sends no DMs, runs no outreach or reminders, posts no
lineup, creates no Notion events and refuses mutating admin commands. Read-only admin commands
and /config keep working, so an organizer can set everything up first and then turn it on with
/config set active on.
"""
from typing import Any

from bot.db.config import get_value, set_value

CONFIG_KEY = "bot_active"

PASSIVE_NOTICE = (
    "The bot is in passive mode, so nothing is sent to DJs. "
    "An organizer can turn it on with /config set active on."
)


async def is_active(pool: Any, guild_id: int | None) -> bool:
    """True only if an organizer switched the bot on for this guild."""
    if pool is None or guild_id is None:
        return False
    return (await get_value(pool, guild_id, CONFIG_KEY)) == "on"


async def set_active(pool: Any, guild_id: int, on: bool) -> None:
    await set_value(pool, guild_id, CONFIG_KEY, "on" if on else "off")

DJ_NOTICE = "I'm not taking bookings right now. Please try again later."
