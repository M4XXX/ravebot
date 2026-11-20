"""Canonical payloads: one shape for Postgres rows and Notion pages.

The mirror worker and the poll compare these (via ``payload_hash``) to decide
whether Notion differs from Postgres. Pure functions, no I/O.
"""
import datetime
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from bot.notion.models import (
    PROP_SLOT_DJ,
    PROP_SLOT_EVENT,
    PROP_SLOT_NUMBER,
    _number,
    _prop,
    _relation_ids,
    _rich,
    parse_dj,
    parse_event,
)

__all__ = [
    "dj_payload",
    "event_payload",
    "notion_dj_payload",
    "notion_event_payload",
    "notion_slot_payload",
    "payload_hash",
    "slot_payload",
]

PROP_DJ_VRCHAT_NAME = "VRChat name"
PROP_DJ_LINK = "Link"
PROP_DJ_ALT_LINK = "Alternative link"


def _iso(value: Any) -> str:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()[:10]
    return str(value)


def event_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "date": _iso(row["date"]),
        "start": row["start"],
        "slot_minutes": row["slot_minutes"],
        "slot_count": row["slot_count"],
        "status": row["status"] or None,
        "genre": row["genre"] or None,
        "notes": row["notes"] or None,
    }


def slot_payload(
    event_uid: str, slot_index: int, discord_user_id: int | None
) -> dict[str, Any]:
    return {
        "event_uid": event_uid,
        "number": slot_index + 1,
        "discord_id": discord_user_id,
    }


def dj_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """The compared DJ fields; logos go Postgres -> Notion only and are not part of it."""
    return {
        "discord_id": row["discord_user_id"],
        "dj_name": row["dj_name"],
        "vrchat_name": row["vrchat_name"] or None,
        "link": row["stream_url_primary"] or None,
        "alt_link": row["stream_url_backup"] or None,
    }


def notion_event_payload(page: dict[str, Any]) -> dict[str, Any] | None:
    event = parse_event(page)
    if event is None:
        return None
    return {
        "event_id": event.event_id,
        "date": event.date.isoformat(),
        "start": event.start.strftime("%H:%M"),
        "slot_minutes": event.slot_minutes,
        "slot_count": event.slot_count,
        "status": event.status or None,
        "genre": event.genre,
        "notes": event.notes,
    }


def notion_slot_payload(
    page: dict[str, Any],
    event_uid_by_page: dict[str, str],
    discord_id_by_dj_page: dict[str, int | None],
) -> dict[str, Any] | None:
    number = _number(page, PROP_SLOT_NUMBER)
    event_ids = _relation_ids(page, PROP_SLOT_EVENT)
    if number is None or number < 1 or not event_ids:
        return None
    event_uid = event_uid_by_page.get(event_ids[0])
    if event_uid is None:
        return None
    dj_ids = _relation_ids(page, PROP_SLOT_DJ)
    return {
        "event_uid": event_uid,
        "number": int(number),
        "discord_id": discord_id_by_dj_page.get(dj_ids[0]) if dj_ids else None,
    }


def _url(page: dict[str, Any], name: str) -> str | None:
    return _prop(page, name).get("url") or None


def notion_dj_payload(page: dict[str, Any]) -> dict[str, Any] | None:
    dj = parse_dj(page)
    if not dj.name or dj.discord_id is None:
        return None
    return {
        "discord_id": dj.discord_id,
        "dj_name": dj.name,
        "vrchat_name": _rich(page, PROP_DJ_VRCHAT_NAME),
        "link": _url(page, PROP_DJ_LINK),
        "alt_link": _url(page, PROP_DJ_ALT_LINK),
    }


def payload_hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()
