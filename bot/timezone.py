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
