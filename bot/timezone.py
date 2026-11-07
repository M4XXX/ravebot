"""Event time zone: wall-clock times are local, instants are timezone-aware."""
import datetime
import logging
import zoneinfo

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Berlin"

_zone: zoneinfo.ZoneInfo = zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)


def get_timezone() -> zoneinfo.ZoneInfo:
    return _zone


def is_valid_timezone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def set_timezone(name: str) -> None:
    """Switch the zone; raises ValueError for an unknown name (zone unchanged)."""
    global _zone
    name = name.strip()
    if not name or not is_valid_timezone(name):
        raise ValueError(f"Unknown time zone: {name!r}")
    _zone = zoneinfo.ZoneInfo(name)


def local_now() -> datetime.datetime:
    return datetime.datetime.now(_zone)


def local_today() -> datetime.date:
    return local_now().date()


async def load_stored_timezone(db, guild_id: int) -> bool:  # noqa: ANN001
    """Apply the guild's stored `timezone` config; keep the default if invalid.

    Returns True when a stored value was applied.
    """
    from bot.db.config import get_value

    raw = await get_value(db, guild_id, "timezone")
    if not raw:
        return False
    try:
        set_timezone(raw)
    except ValueError:
        logger.warning(
            "Stored timezone %r is invalid; keeping %s", raw, get_timezone().key
        )
        return False
    return True


async def load_stored_timezone_for_bot(bot, db) -> None:  # noqa: ANN001
    """Apply the stored zone of the first guild; never raises (used by loop before_loop)."""
    if db is None or bot is None or not bot.guilds:
        return
    try:
        await load_stored_timezone(db, bot.guilds[0].id)
    except Exception:
        logger.warning("Could not load the stored time zone", exc_info=True)
