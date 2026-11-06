"""Create the three Notion views (Events "Overview", Slots "Lineup", DJs "All DJs").

``ensure_views`` lists the existing views of each database by name and creates
only the missing ones. Existing views (including Notion's "Default view") are
never modified or deleted. The ``notion_views_created`` marker is set only when
all three exist. Request shapes come from the API spike
(tests/notion/fixtures/NOTES.md, views_real.json).
"""
import logging
from typing import Any

import asyncpg

from bot.db.config import get_value, set_value
from bot.notion.api import NotionApi
from bot.notion.schema import NotionIds

logger = logging.getLogger(__name__)

_MARKER_KEY = "notion_views_created"

_DJ_COLUMNS = (
    "DJ name", "Discord name", "VRChat name", "Link", "Alternative link",
    "Events booked", "Last booked",
)


def _visible(*names: str) -> list[dict[str, Any]]:
    return [{"property_id": n, "visible": True} for n in names]


def _view_specs(ids: NotionIds) -> list[tuple[str, dict[str, Any]]]:
    """(database_id, request body) per view."""
    return [
        (ids.events_db, {
            "name": "Overview", "type": "table",
            "sorts": [{"property": "Date", "direction": "descending"}],
            "database_id": ids.events_db, "data_source_id": ids.events_ds,
        }),
        (ids.slots_db, {
            "name": "Lineup", "type": "table",
            "sorts": [{"property": "Slot number", "direction": "ascending"}],
            "configuration": {
                "type": "table",
                "group_by": {
                    "type": "relation",
                    "property_id": ids.slots_event_property_id,
                    "sort": {"type": "descending"},
                },
                "properties": _visible("Slot number", "Time", "DJ"),
            },
            "database_id": ids.slots_db, "data_source_id": ids.slots_ds,
        }),
        (ids.djs_db, {
            "name": "All DJs", "type": "table",
            "sorts": [{"property": "Last booked", "direction": "descending"}],
            "configuration": {"type": "table", "properties": _visible(*_DJ_COLUMNS)},
            "database_id": ids.djs_db, "data_source_id": ids.djs_ds,
        }),
    ]


async def _view_names(api: NotionApi, database_id: str) -> set[str]:
    names: set[str] = set()
    cursor: str | None = None
    while True:
        path = f"/views?database_id={database_id}"
        if cursor:
            path += f"&start_cursor={cursor}"
        page = await api.request("GET", path, None, f"list views of {database_id}")
        for item in page.get("results", []):
            view = await api.request("GET", f"/views/{item['id']}", None, "read view")
            names.add(view.get("name", ""))
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            return names


async def ensure_views(
    api: NotionApi, db: asyncpg.Pool, guild_id: int, ids: NotionIds
) -> None:
    if await get_value(db, guild_id, _MARKER_KEY) == "1":
        return
    existing: dict[str, set[str]] = {}
    for database_id, body in _view_specs(ids):
        if database_id not in existing:
            existing[database_id] = await _view_names(api, database_id)
        if body["name"] in existing[database_id]:
            continue
        await api.request("POST", "/views", body, f"create view {body['name']}")
        logger.info("Created Notion view %s", body["name"])
    await set_value(db, guild_id, _MARKER_KEY, "1")
