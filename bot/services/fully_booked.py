"""Announce events that became fully booked, once per fill.

check_fully_booked() is called from the poll cycle and after every booking or
cancellation. The announced flag lives in Postgres (fully_booked_events), so a
restart, a double click or two paths filling at once announce an event once.
"""
import datetime
import logging

import asyncpg

from bot.db import fully_booked as fb_db
from bot.log import AdminLogService
from bot.notion.models import EventInfo

logger = logging.getLogger(__name__)


def is_fully_booked(event: EventInfo) -> bool:
    """True when the event has slots and every number 1..slot_count holds a DJ."""
    if event.slot_count <= 0:
        return False
    slots = event.resolved_slots()
    for number in range(1, event.slot_count + 1):
        slot = slots.get(number)
        if slot is None or slot.dj_page_id is None:
            return False
    return True


async def check_fully_booked(
    db: asyncpg.Pool,
    log_service: AdminLogService | None,
    events: list[EventInfo],
    *,
    listed_at: datetime.datetime | None = None,
) -> None:
    """Announce newly full events and clear the flag of events that opened up.

    listed_at is the time just before `events` was read: a flag set after that
    (by a booking path) is newer than the list and is not cleared by it.

    Never raises: a failure is logged and the caller carries on.
    """
    try:
        announced = await fb_db.get_announced(db, [e.page_id for e in events])
        for event in events:
            full = is_fully_booked(event)
            if full and event.page_id not in announced:
                if await fb_db.mark_announced(db, event.page_id) and log_service is not None:
                    await log_service.log_fully_booked(event)
            elif not full and event.page_id in announced:
                await fb_db.clear_announced(db, event.page_id, before=listed_at)
    except Exception:
        logger.exception("check_fully_booked failed")


async def recheck_fully_booked(ctx_db, log_service, notion_client) -> None:
    """Reload the events and run check_fully_booked; never raises.

    Called after a booking or cancellation (the caller's event list predates it).
    """
    if ctx_db is None or notion_client is None:
        return
    listed_at = datetime.datetime.now(datetime.UTC)
    try:
        events = await notion_client.list_events()
    except Exception:
        logger.exception("recheck_fully_booked: list_events failed")
        return
    await check_fully_booked(ctx_db, log_service, events, listed_at=listed_at)
