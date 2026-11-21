"""Keep the configured number of future events ready.

Events are written to Postgres; the Notion mirror follows in the background.
Used by the poll loop (EventsCog) and by the setup wizard at activation time.
"""
import asyncio
import datetime
import logging
import weakref
from collections.abc import Iterator
from datetime import timedelta
from itertools import count, islice
from typing import TYPE_CHECKING

from bot.db import events as db_events
from bot.db.config import get_value, set_value
from bot.db.manual_events import get_manual_page_ids
from bot.notion.models import FINISHED_STATUSES, EventInfo
from bot.services import schedule as schedule_service
from bot.services.event_defaults import default_event_shape
from bot.sync.worker import changed
from bot.timezone import local_today

if TYPE_CHECKING:
    from bot.context import BotContext

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

def replenish_guard() -> asyncio.Lock:
    """The lock that serializes event creation: hold it while creating an event by hand."""
    return _replenish_lock()


def mirror_new_event(ctx: "BotContext", event: EventInfo) -> None:
    """Queue a new event and each of its slots for the Notion mirror."""
    changed(ctx, "event", event.page_id)
    for index in range(event.slot_count):
        changed(ctx, "slot", f"{event.page_id}:{index}")


FIRST_DATE_KEY = "first_event_date"


def scheduled_event_id(day: datetime.date) -> str:
    """The event ID replenish gives the event on `day`."""
    return f"{day.isoformat()} Rave Night"

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
    ctx: "BotContext",
    guild_id: int,
    events: list[EventInfo] | None = None,
) -> int:
    """Serialized wrapper: the poll loop and the wizard never replenish at the same time.

    Without `events` the list is loaded inside the lock, so it is never stale.
    Creates nothing (0) without a database or while in maintenance (an import
    replaces the events).
    """
    if ctx.db is None or ctx.maintenance:
        return 0
    async with _replenish_lock():
        if ctx.maintenance:  # an import or rebuild started while this waited
            return 0
        if events is None:
            loaded: list[EventInfo] = await db_events.list_events(ctx.db)
        else:
            loaded = events
        return await _replenish(ctx, guild_id, loaded)


async def _replenish(ctx: "BotContext", guild_id: int, events: list[EventInfo]) -> int:
    """Create scheduled events until the look-ahead count of future, open ones is met.

    Does nothing when the schedule is off. Manual events (/create event) are ignored:
    they neither count nor move the base date. Scheduled events are active with a local
    date of today or later and a status other than Done or Canceled.

    New events follow the schedule: with a valid first_event_date and no scheduled event
    dated today or later, the first one gets that date; otherwise they start one interval
    after max(latest scheduled event date, today). A date held by a manual event is
    skipped and the next date is used; a date held by any other event is spent without
    a creation. first_event_date is cleared once it was used or found unusable.
    Each new event gets the genre and location the schedule rules and defaults give
    its date (rules read once per run), hand markers false.
    Returns the number created.
    """
    db = ctx.db
    assert db is not None
    if not _schedule_enabled(await get_value(db, guild_id, "event_schedule_enabled")):
        return 0

    lookahead = _to_int(await get_value(db, guild_id, "event_lookahead_count"), 6)
    today = local_today()
    manual = await get_manual_page_ids(db, [e.page_id for e in events])
    scheduled = [e for e in events if e.page_id not in manual]
    active = [e for e in scheduled if e.date >= today and e.status not in FINISHED_STATUSES]
    missing = lookahead - len(active)
    if missing <= 0:
        return 0

    interval_days = max(1, _to_int(await get_value(db, guild_id, "event_interval_days"), 7))
    raw_first = await get_value(db, guild_id, FIRST_DATE_KEY)
    first = _parse_first_date(raw_first, today)
    if any(e.date >= today for e in scheduled):
        first = None
    base: datetime.date = max([e.date for e in scheduled] + [today])
    shape = await default_event_shape(db, guild_id)
    resolved = await schedule_service.load_resolver(db, guild_id, interval_days)
    manual_dates = {e.date for e in events if e.page_id in manual}

    created = 0
    attempts = 0
    for new_date in islice(_candidate_dates(first, base, interval_days), missing + _SKIP_ALLOWANCE):
        if attempts >= missing:
            break
        event_id = scheduled_event_id(new_date)
        counted = False
        try:
            if await db_events.event_id_exists(db, event_id):
                logger.info("Auto-create skipped: event ID %r already exists", event_id)
                if new_date in manual_dates:
                    continue  # a manual event holds this date: the next date is used
                attempts += 1  # unknown event (stale list): this date is spent, as before
                counted = True
                continue
            attempts += 1
            counted = True
            draft = db_events.EventDraft(
                event_id=event_id,
                date=new_date,
                start=shape.start,
                slot_minutes=shape.slot_minutes,
                slot_count=shape.slot_count,
                genre=resolved("genre", new_date),
                location=resolved("location", new_date),
            )
            event = await db_events.insert_event(db, draft)
            logger.info("Auto-created event: %r", event_id)
            created += 1
            mirror_new_event(ctx, event)
        except db_events.DuplicateEventError:
            logger.warning("Auto-create skipped: event ID %r was just taken", event_id)
        except Exception:
            if not counted:
                attempts += 1
            logger.exception("Auto event creation failed for %s", new_date.isoformat())
    if (raw_first or "").strip() and (first is None or created > 0):
        await set_value(db, guild_id, FIRST_DATE_KEY, "")
    return created
