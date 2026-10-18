"""Read-only check that Notion and the booking database agree.

Bookings live in two places: slot_locks in Postgres (authoritative) and the DJs relation on
the Notion event page (tds layout: the DJ's name in the slot's column). After a failed Notion
write, a cancellation that freed the slot anyway, or a hand edit in Notion they can drift apart.
This module only REPORTS differences to the admin log, once per distinct set of differences; it
never writes to Notion or the database.
"""
import hashlib
import json
import logging
from typing import Any

from bot import slots
from bot.db.bookings import get_booked_slots
from bot.db.config import get_value, set_value
from bot.db.djs import get_dj_names, get_notion_page_ids
from bot.notion import props
from bot.ui.embeds import ErrorCode

logger = logging.getLogger(__name__)

CONFIG_KEY = "reconcile_last_report"

# Events already logged as too large to check (logged once per process, not every poll).
_skipped_large: set[str | None] = set()


def _norm(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def _name(event: dict) -> str:
    try:
        return props.title_text(event)
    except (KeyError, IndexError, TypeError):
        return event.get("id", "?")


async def _plan_mismatches(pool: Any, events: list[dict]) -> list[dict]:
    """tds layout: booked slots whose column does not show the booked DJ's name.

    A name typed into a column without a booking is how organizers plan, not a difference.
    """
    booked = await get_booked_slots(pool, [e["id"] for e in events if "id" in e])
    names = await get_dj_names(pool, sorted({u for s in booked.values() for u in s.values()}))
    result = []
    for event in events:
        missing = [
            f"slot {index + 1}: {names[uid]}"
            for index, uid in sorted(booked.get(event.get("id", ""), {}).items())
            if uid in names and slots.cell_text(event, index) != names[uid]
        ]
        if missing:
            result.append(
                {"event": _name(event), "missing_in_notion": missing, "only_in_notion": []}
            )
    return result


async def find_mismatches(pool: Any, events: list[dict]) -> list[dict]:
    """Per event: DJs booked in the database but not linked in Notion, and the reverse."""
    if slots.layout() == slots.TDS:
        return await _plan_mismatches(pool, events)
    booked = await get_booked_slots(pool, [e["id"] for e in events if "id" in e])
    holders = sorted({uid for slots in booked.values() for uid in slots.values()})
    pages = await get_notion_page_ids(pool, holders)
    result = []
    for event in events:
        try:
            djs = event["properties"]["DJs"]
            linked = {_norm(r["id"]) for r in djs["relation"]}
        except (KeyError, TypeError):
            continue
        if djs.get("has_more"):
            # A database query returns at most 25 related pages; comparing the cut list
            # would report every DJ beyond it as "not linked".
            if event.get("id") not in _skipped_large:
                _skipped_large.add(event.get("id"))
                logger.warning(
                    "Reconcile does not check %s: more than 25 linked DJs", _name(event)
                )
            continue
        holders_here = booked.get(event.get("id", ""), {}).values()
        in_db = {_norm(pages[uid]) for uid in holders_here if uid in pages}
        missing_in_notion, only_in_notion = sorted(in_db - linked), sorted(linked - in_db)
        if missing_in_notion or only_in_notion:
            result.append({
                "event": _name(event),
                "missing_in_notion": missing_in_notion,
                "only_in_notion": only_in_notion,
            })
    return result


async def report(pool: Any, guild_id: int, events: list[dict], log_service: Any) -> list[dict]:
    """Post differences to the admin log when they changed since the last report."""
    mismatches = await find_mismatches(pool, events)
    digest = hashlib.sha256(json.dumps(mismatches, sort_keys=True).encode()).hexdigest()[:16]
    if digest == await get_value(pool, guild_id, CONFIG_KEY):
        return mismatches
    await set_value(pool, guild_id, CONFIG_KEY, digest)
    if not mismatches or log_service is None:
        return mismatches
    lines = []
    for m in mismatches[:10]:
        parts = []
        if m["missing_in_notion"] and slots.layout() == slots.TDS:
            parts.append("not in the plan: " + "; ".join(m["missing_in_notion"]))
        elif m["missing_in_notion"]:
            parts.append(f"{len(m['missing_in_notion'])} booked DJ(s) not linked in Notion")
        if m["only_in_notion"]:
            parts.append(f"{len(m['only_in_notion'])} linked in Notion without a booking")
        lines.append(f"{m['event']}: " + ", ".join(parts))
    logger.warning("Notion and slot_locks disagree: %s", lines)
    await log_service.post_error(
        error_code=ErrorCode.ERR_DATA_MISMATCH,
        trigger="Notion and the booking database disagree:\n" + "\n".join(lines),
        action_taken="Nothing was changed — the bot never fixes this on its own.",
        manual_action="Compare the event in Notion with /ravebot-status and correct it by hand.",
    )
    return mismatches
