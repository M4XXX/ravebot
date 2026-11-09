"""Keep the configured number of future events ready in Notion.

Used by the poll loop (EventsCog) and by the setup wizard at activation time.
"""
import asyncio
import datetime
import logging
import weakref
from collections.abc import Iterator
from datetime import timedelta
from itertools import count, islice

from bot.db.bookings import init_event_slots
from bot.db.config import get_value, set_value
from bot.db.manual_events import get_manual_page_ids
from bot.notion.client import DuplicateEventError, NotionWriteError
from bot.notion.models import EventInfo
from bot.services.event_defaults import default_event_shape
from bot.timezone import local_today

logger = logging.getLogger(__name__)

# Shared by the poll loop and the setup wizard: create-if-missing must not run twice at once.
_replenish_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _replenish_lock() -> asyncio.Lock:
    """The lock of the running event loop (one loop in production; tests use many)."""
    loop = asyncio.get_running_loop()
    lock = _replenish_locks.get(loop)
    if lock is None:
        lock = _replenish_locks[loop] = asyncio.Lock()
    return lock

_FINISHED_STATUSES = {"Done", "Canceled"}
FIRST_DATE_KEY = "first_event_date"
# Extra dates tried beyond `missing` when some dates already hold an event.
_SKIP_ALLOWANCE = 52


def _schedule_enabled(raw: str | None) -> bool:
    """Missing or anything other than "false" means on."""
    return raw is None or raw.strip().lower() != "false"


def _parse_first_date(raw: str | None, today: datetime.date) -> datetime.date | None:
    """The stored first event date when it is valid and today or later, else None."""
    try:
        day = datetime.date.fromisoformat((raw or "").strip())
    except ValueError:
        return None
    return day if day >= today else None


def _candidate_dates(
    first: datetime.date | None, base: datetime.date, interval_days: int
) -> Iterator[datetime.date]:
    """Scheduled dates in order: first + n * interval, or base + n * interval from n = 1."""
    if first is not None:
        return (first + timedelta(days=interval_days * n) for n in count(0))
    return (base + timedelta(days=interval_days * n) for n in count(1))


def _to_int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


async def replenish_events(
    db,  # noqa: ANN001
    notion_client,  # noqa: ANN001
    guild_id: int,
    events: list[EventInfo] | None = None,
) -> int:
    """Serialized wrapper: the poll loop and the wizard never replenish at the same time.

    Without `events` the list is loaded inside the lock, so it is never stale.
    """
    async with _replenish_lock():
        if events is None:
            if notion_client is None:
                return 0
            loaded: list[EventInfo] = await notion_client.list_events()
        else:
            loaded = events
        return await _replenish(db, notion_client, guild_id, loaded)


async def _replenish(db, notion_client, guild_id: int, events: list[EventInfo]) -> int:  # noqa: ANN001
    """Create scheduled events until the look-ahead count of future, open ones is met.

    Does nothing when the schedule is off. Manual events (/create event) are ignored:
    they neither count nor move the base date. Scheduled events are active with a local
    date of today or later and a status other than Done or Canceled.

    New events follow the schedule: with a valid first_event_date and no scheduled event
    dated today or later, the first one gets that date; otherwise they start one interval
    after max(latest scheduled event date, today). A date held by a manual event is
    skipped and the next date is used; a date held by any other event is spent without
    a creation. first_event_date is cleared once it was used or found unusable.
    Returns the number created.
    """
    if notion_client is None or db is None:
        return 0
    if not _schedule_enabled(await get_value(db, guild_id, "event_schedule_enabled")):
        return 0

    lookahead = _to_int(await get_value(db, guild_id, "event_lookahead_count"), 6)
    today = local_today()
    manual = await get_manual_page_ids(db, [e.page_id for e in events])
    scheduled = [e for e in events if e.page_id not in manual]
    active = [e for e in scheduled if e.date >= today and e.status not in _FINISHED_STATUSES]
    missing = lookahead - len(active)
    if missing <= 0:
        return 0

    interval_days = _to_int(await get_value(db, guild_id, "event_interval_days"), 7)
    raw_first = await get_value(db, guild_id, FIRST_DATE_KEY)
    first = _parse_first_date(raw_first, today)
    if any(e.date >= today for e in scheduled):
        first = None
    base: datetime.date = max([e.date for e in scheduled] + [today])
    shape = await default_event_shape(db, guild_id)
    manual_dates = {e.date for e in events if e.page_id in manual}

    created = 0
    attempts = 0
    for new_date in islice(_candidate_dates(first, base, interval_days), missing + _SKIP_ALLOWANCE):
        if attempts >= missing:
            break
        event_id = f"{new_date.isoformat()} Rave Night"
        counted = False
        try:
            if await notion_client.event_id_exists(event_id):
                logger.info("Auto-create skipped: event ID %r already exists", event_id)
                if new_date in manual_dates:
                    continue  # a manual event holds this date: the next date is used
                attempts += 1  # unknown event (stale list): this date is spent, as before
                counted = True
                continue
            attempts += 1
            counted = True
            event = await notion_client.create_event(
                event_id, new_date, shape.start, shape.slot_count, shape.slot_minutes
            )
            logger.info("Auto-created Notion event: %r", event_id)
            created += 1
            try:
                await init_event_slots(db, event.page_id, shape.slot_count)
            except Exception as exc:
                logger.warning(
                    "init_event_slots failed for event %r: %s"
                    " — slots will be lazily created on booking",
                    event_id,
                    exc,
                )
        except (NotionWriteError, DuplicateEventError):
            if not counted:
                attempts += 1
            logger.warning(
                "Auto event creation failed for %s — ERR_NOTION_WRITE posted to admin log",
                new_date.isoformat(),
            )
        except ValueError:
            logger.exception("Auto event creation rejected for %s", new_date.isoformat())
    if (raw_first or "").strip() and (first is None or created > 0):
        await set_value(db, guild_id, FIRST_DATE_KEY, "")
    return created
