"""Which Discord server the bot serves — one place instead of bot.guilds[0] everywhere.

With GUILD_ID set, only that server is served. Without it, the bot serves the single server it
is in; in several servers it serves none and logs why, instead of picking one at random.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

_pinned: int | None = None
_warned: set[str] = set()


def configure(guild_id: int | None) -> None:
    """Pin the served guild (from Config.guild_id). None means: the only guild the bot is in."""
    global _pinned
    _pinned = guild_id
    _warned.clear()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.error(msg, *args)


def resolve(bot: Any) -> int | None:
    """Return the ID of the guild this bot serves, or None if it serves none right now."""
    guilds = list(getattr(bot, "guilds", None) or [])
    if _pinned is not None:
        if any(g.id == _pinned for g in guilds):
            return _pinned
        if guilds:
            _warn_once("missing", "GUILD_ID %s: bot is not a member of that server", _pinned)
        return None
    if len(guilds) == 1:
        return guilds[0].id
    if len(guilds) > 1:
        _warn_once(
            "many", "Bot is in %d servers and GUILD_ID is not set — serving none", len(guilds)
        )
    return None


def serves(bot: Any, guild_id: int | None) -> bool:
    """True if guild_id is the guild this bot serves (events from other guilds are ignored)."""
    return guild_id is not None and guild_id == resolve(bot)


def served_guild(bot: Any) -> Any:
    """The served disnake.Guild object, or None."""
    gid = resolve(bot)
    if gid is None:
        return None
    return next((g for g in bot.guilds if g.id == gid), None)


def served_guilds(bot: Any) -> list:
    """The served guild as a 0- or 1-element list, for code that used to loop over bot.guilds."""
    g = served_guild(bot)
    return [g] if g is not None else []
