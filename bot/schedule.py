"""Event and slot start times — the one place that turns a Notion event date into datetimes.

Notion stores event dates without a time of day. The start time, slot length and time zone come
from the config table (/config set start-time, slot-duration, timezone). They are cached here so
synchronous UI code (embeds, views) can compute slot times without a DB round trip.

Booking views, the lineup post, /status, reminders and outreach all call event_start() and
slot_start(), so a DJ is always shown the same time the reminder is sent for.
"""
import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.db.config import get_value

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Schedule:
    """Time zone, first-slot start time and slot length for a guild's events."""

    tz: ZoneInfo
    start: dt.time
    slot_minutes: int


DEFAULT_SCHEDULE = Schedule(tz=ZoneInfo("UTC"), start=dt.time(20, 0), slot_minutes=60)

_current = DEFAULT_SCHEDULE


def current() -> Schedule:
    """Return the most recently loaded schedule (DEFAULT_SCHEDULE until refresh() ran)."""
    return _current


def today(sched: Schedule | None = None) -> dt.date:
    """Today's date in the configured time zone (/config set timezone; UTC until refresh()).

    Use this instead of the stdlib date.today, which is the server's date: around midnight the two
    differ, and events would vanish from the lineup or be refused a few hours early.
    [Source: epics.md#Story 8.7]
    """
    return dt.datetime.now((sched or current()).tz).date()


def parse_timezone(name: str | None) -> ZoneInfo | None:
    """Return the ZoneInfo for an IANA name like 'Europe/Berlin', or None if unknown."""
    if not name:
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def parse_schedule(
    timezone_name: str | None, start_str: str | None, slot_str: str | None
) -> Schedule:
    """Build a Schedule from raw config values, falling back to defaults field by field."""
    tz = parse_timezone(timezone_name) or DEFAULT_SCHEDULE.tz
    try:
        hour, minute = (int(x) for x in (start_str or "").split(":"))
        start = dt.time(hour, minute)
    except (ValueError, TypeError):
        start = DEFAULT_SCHEDULE.start
    try:
        slot_minutes = int(slot_str) if slot_str else DEFAULT_SCHEDULE.slot_minutes
        if slot_minutes <= 0:
            raise ValueError
    except (ValueError, TypeError):
        slot_minutes = DEFAULT_SCHEDULE.slot_minutes
    return Schedule(tz=tz, start=start, slot_minutes=slot_minutes)


async def refresh(pool: "asyncpg.Pool", guild_id: int) -> Schedule:
    """Reload the schedule from the config table and make it the cached one."""
    global _current
    _current = parse_schedule(
        await get_value(pool, guild_id, "timezone"),
        await get_value(pool, guild_id, "default_start_time"),
        await get_value(pool, guild_id, "slot_duration_minutes"),
    )
    return _current


def event_start(event: dict, schedule: Schedule | None = None) -> dt.datetime | None:
    """Return the event's first-slot start as an aware UTC datetime, or None if unparseable.

    A date-only Notion value (the bot's own events) gets the configured start time in the
    configured time zone. A value that already carries a time is used as entered.
    """
    sched = schedule or _current
    try:
        raw = event["properties"]["Date"]["date"]["start"]
        if "T" in raw:
            start = dt.datetime.fromisoformat(raw)
            if start.tzinfo is None:
                start = start.replace(tzinfo=sched.tz)
        else:
            day = dt.date.fromisoformat(raw)
            start = dt.datetime.combine(day, sched.start, tzinfo=sched.tz)
    except (KeyError, TypeError, ValueError):
        return None
    return start.astimezone(dt.UTC)


def slot_start(
    event: dict, slot_index: int, schedule: Schedule | None = None
) -> dt.datetime | None:
    """Return the start of slot `slot_index` (0-based) as an aware UTC datetime, or None.

    The night starts at its local clock time (event_start); its slots follow in real time,
    each exactly slot_minutes long. Adding in wall time instead gave two slots the same
    start on the night the clocks go forward and a one-hour hole when they go back.
    """
    sched = schedule or _current
    start = event_start(event, sched)
    if start is None:
        return None
    return start + dt.timedelta(minutes=slot_index * sched.slot_minutes)


def slot_unix(event: dict, slot_index: int, schedule: Schedule | None = None) -> int:
    """Unix timestamp of a slot start, 0 if the event date is unparseable."""
    start = slot_start(event, slot_index, schedule)
    return int(start.timestamp()) if start is not None else 0


def plain_label(unix: int, schedule: Schedule | None = None) -> str:
    """Readable slot time for places that cannot render <t:...> markup, e.g. button labels."""
    sched = schedule or _current
    local = dt.datetime.fromtimestamp(unix, tz=dt.UTC).astimezone(sched.tz)
    return local.strftime("%a %d %b %H:%M %Z")
