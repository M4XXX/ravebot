"""Default shape (start, slot length, slot count) for events the bot creates."""
import datetime
import logging
import re
from dataclasses import dataclass

import asyncpg

from bot.db.config import get_value
from bot.notion.models import MAX_SLOTS

logger = logging.getLogger(__name__)

DEFAULT_START = datetime.time(20, 0)
DEFAULT_SLOT_MINUTES = 60
DEFAULT_SLOT_COUNT = 6

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class EventShape:
    start: datetime.time
    slot_minutes: int
    slot_count: int


def _parse_start(raw: str | None) -> datetime.time:
    if not raw:
        return DEFAULT_START
    match = _HHMM_RE.match(raw.strip())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            return datetime.time(hour, minute)
    logger.warning(
        "Invalid default_start_time config %r — using default %s",
        raw,
        DEFAULT_START.strftime("%H:%M"),
    )
    return DEFAULT_START


def _parse_positive(
    key: str, raw: str | None, default: int, maximum: int | None = None
) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value < 1 or (maximum is not None and value > maximum):
        logger.warning("Invalid %s config %r — using default %d", key, raw, default)
        return default
    return value


async def default_event_shape(db: asyncpg.Pool, guild_id: int) -> EventShape:
    """Read the configured shape for new events; invalid values fall back to defaults.

    Keys: default_start_time (HH:MM, default 20:00), slot_duration_minutes
    (default 60), slots_per_event (default 6, at most MAX_SLOTS = 100).
    """
    raw_start = await get_value(db, guild_id, "default_start_time")
    raw_minutes = await get_value(db, guild_id, "slot_duration_minutes")
    raw_slots = await get_value(db, guild_id, "slots_per_event")
    return EventShape(
        start=_parse_start(raw_start),
        slot_minutes=_parse_positive("slot_duration_minutes", raw_minutes, DEFAULT_SLOT_MINUTES),
        slot_count=_parse_positive(
            "slots_per_event", raw_slots, DEFAULT_SLOT_COUNT, maximum=MAX_SLOTS
        ),
    )
