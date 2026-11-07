"""Keep the configured number of future events ready in Notion.

Used by the poll loop (EventsCog) and by the setup wizard at activation time.
"""
import datetime
import logging
from datetime import timedelta

from bot.db.bookings import init_event_slots
from bot.db.config import get_value
from bot.notion.client import DuplicateEventError, NotionWriteError
from bot.notion.models import EventInfo
from bot.services.event_defaults import default_event_shape
from bot.timezone import local_today

logger = logging.getLogger(__name__)

_FINISHED_STATUSES = {"Done", "Canceled"}


def _to_int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


async def replenish_events(db, notion_client, guild_id: int, events: list[EventInfo]) -> int:  # noqa: ANN001
    """Create events until the look-ahead count of future, open events is met.

    Active events have a local date of today or later and a status other than
    Done or Canceled. New events start one interval after
    max(latest event date of all events, today). Returns the number created.
    """
    if notion_client is None or db is None:
        return 0

    lookahead = _to_int(await get_value(db, guild_id, "event_lookahead_count"), 6)
    today = local_today()
    active = [e for e in events if e.date >= today and e.status not in _FINISHED_STATUSES]
    missing = lookahead - len(active)
    if missing <= 0:
        return 0

    interval_days = _to_int(await get_value(db, guild_id, "event_interval_days"), 7)
    base: datetime.date = max([e.date for e in events] + [today])
    shape = await default_event_shape(db, guild_id)

    created = 0
    for i in range(1, missing + 1):
        new_date = base + timedelta(days=interval_days * i)
        event_id = f"{new_date.isoformat()} Rave Night"
        try:
            if await notion_client.event_id_exists(event_id):
                logger.info("Auto-create skipped: event ID %r already exists", event_id)
                continue
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
            logger.warning(
                "Auto event creation failed for %s — ERR_NOTION_WRITE posted to admin log",
                new_date.isoformat(),
            )
        except ValueError:
            logger.exception("Auto event creation rejected for %s", new_date.isoformat())
    return created
