"""Activation state: the bot stays idle until it is activated for its guild.

`bot_active` (config key, per guild) is "true" when active. A guild with both
`dj_channel` and `log_channel` stored and no `bot_active` key counts as active, so
servers set up before the setup command existed keep working.
"""
from typing import Any

from bot.db.config import get_value, set_value

ACTIVE_KEY = "bot_active"


async def is_active(db: Any, guild_id: int) -> bool:
    """True when the guild is active (explicit flag, or both channels stored)."""
    flag = await get_value(db, guild_id, ACTIVE_KEY)
    if flag is not None:
        return flag == "true"
    dj_channel = await get_value(db, guild_id, "dj_channel")
    log_channel = await get_value(db, guild_id, "log_channel")
    return dj_channel is not None and log_channel is not None


async def set_active(db: Any, guild_id: int) -> None:
    """Mark the guild as active."""
    await set_value(db, guild_id, ACTIVE_KEY, "true")


async def bot_is_active(bot: Any, db: Any) -> bool:
    """Activation state of the first guild; False without a guild or database.

    For code that may run in DMs, where no guild id is at hand.
    """
    if db is None or bot is None or not bot.guilds:
        return False
    return await is_active(db, bot.guilds[0].id)
