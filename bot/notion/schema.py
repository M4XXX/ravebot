"""Create and verify the three Notion databases (Events, Slots, DJs).

``ensure_schema`` is idempotent: it reads the stored data source ids from the
config table, verifies every property, adds what is missing, recreates data
sources that no longer exist (404) and refuses to touch properties that have
the wrong type (reported to the admin log, raises NotionSchemaError).
A fresh set of databases also clears the stored DJ page ids and the views
marker, so ensure_views creates the views in the new databases.

Request shapes and formula expressions come from the API spike
(tests/notion/fixtures/NOTES.md).
"""
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import asyncpg

from bot.db.config import get_value, set_value
from bot.db.djs import clear_all_notion_dj_page_ids
from bot.notion.api import NotionApi, NotionWriteError
from bot.ui import texts

logger = logging.getLogger(__name__)

STATUS_OPTIONS = ("Planning not started", "Planning stage", "Ready", "Done", "Canceled")

TIME_FORMULA = (
    'lets(m, mod(toNumber(prop("Event start").substring(0, 2)) * 60 + '
    'toNumber(prop("Event start").substring(3, 5)) + '
    '(prop("Slot number") - 1) * prop("Event slot length"), 1440), '
    'format(floor(m / 60)).padStart(2, "0") + ":" + format(m % 60).padStart(2, "0"))'
)
SLOT_STATUS_FORMULA = 'if(empty(prop("DJ")), "Open", "Booked")'

_KEYS = {
    "events_ds": "notion_events_ds",
    "slots_ds": "notion_slots_ds",
    "djs_ds": "notion_djs_ds",
    "events_db": "notion_events_db",
    "slots_db": "notion_slots_db",
    "djs_db": "notion_djs_db",
    "slots_event_property_id": "notion_slots_event_prop",
}


_COMPLETE_KEY = "notion_schema_complete"
# Set by bot.notion.views.ensure_views once all views exist; cleared for a fresh set.
VIEWS_MARKER_KEY = "notion_views_created"


class NotionSchemaError(Exception):
    """A Notion database does not match the expected schema."""


@dataclass(frozen=True)
class NotionIds:
    events_ds: str
    slots_ds: str
    djs_ds: str
    events_db: str
    slots_db: str
    djs_db: str
    slots_event_property_id: str  # URL-decoded; needed for the Lineup view group_by


def _events_props() -> dict[str, Any]:
    return {
        "ID": {"title": {}},
        "Date": {"date": {}},
        "Status": {"select": {"options": [{"name": n} for n in STATUS_OPTIONS]}},
        "Genre": {"rich_text": {}},
        "Notes": {"rich_text": {}},
        "Slots": {"number": {}},
        "Start": {"rich_text": {}},
        "Slot length": {"number": {}},
    }


def _djs_props() -> dict[str, Any]:
    return {
        "DJ name": {"title": {}},
        "Discord name": {"rich_text": {}},
        "Discord ID": {"rich_text": {}},
        "VRChat name": {"rich_text": {}},
        "Logo": {"files": {}},
        "Link": {"url": {}},
        "Alternative link": {"url": {}},
    }


def _slots_props(events_ds: str, djs_ds: str, *, dj_dual: bool) -> dict[str, Any]:
    dj_rel: dict[str, Any] = {"data_source_id": djs_ds}
    if dj_dual:
        dj_rel["dual_property"] = {"synced_property_name": "Slots"}
    else:
        dj_rel["single_property"] = {}
    return {
        "Name": {"title": {}},
        "Slot number": {"number": {}},
        "Event": {
            "relation": {
                "data_source_id": events_ds,
                "dual_property": {"synced_property_name": "Slot rows"},
            }
        },
        "DJ": {"relation": dj_rel},
    }


def _rollup(relation: str, prop: str, function: str) -> dict[str, Any]:
    return {
        "rollup": {
            "relation_property_name": relation,
            "rollup_property_name": prop,
            "function": function,
        }
    }


def _slots_rollups() -> dict[str, Any]:
    return {
        "Event start": _rollup("Event", "Start", "show_original"),
        "Event slot length": _rollup("Event", "Slot length", "max"),
        "Event date src": _rollup("Event", "Date", "latest_date"),
    }


def _slots_formulas() -> dict[str, Any]:
    return {
        "Event date": {"formula": {"expression": 'prop("Event date src")'}},
        "Time": {"formula": {"expression": TIME_FORMULA}},
        "Slot status": {"formula": {"expression": SLOT_STATUS_FORMULA}},
    }


def _djs_rollups() -> dict[str, Any]:
    return {
        "Events booked": _rollup("Slots", "Event", "unique"),
        "Last booked": _rollup("Slots", "Event date", "latest_date"),
    }


def _kind(spec: dict[str, Any]) -> str:
    return next(iter(spec))


def _relation_state(existing: dict[str, Any], spec: dict[str, Any]) -> str:
    """Return "ok", "upgrade" (single -> dual on the right target) or "wrong"."""
    want = spec["relation"]
    have = existing.get("relation", {})
    if have.get("data_source_id") != want["data_source_id"]:
        return "wrong"
    if "dual_property" not in want:
        return "ok"
    if have.get("type") == "single_property":
        return "upgrade"
    dual = have.get("dual_property", {})
    same = dual.get("synced_property_name") == want["dual_property"]["synced_property_name"]
    return "ok" if same else "wrong"


async def _reconcile(
    api: NotionApi, ds_id: str, label: str, desired: dict[str, Any]
) -> dict[str, Any]:
    """Verify ``desired`` properties on a data source; add what is missing."""
    ds = await api.request("GET", f"/data_sources/{ds_id}", None, f"retrieve {label} data source")
    props: dict[str, Any] = ds.get("properties", {})
    wrong: list[str] = []
    to_patch: dict[str, Any] = {}
    for name, spec in desired.items():
        kind = _kind(spec)
        have = props.get(name)
        if have is None:
            if kind == "title":
                other = next((n for n, p in props.items() if p["type"] == "title"), None)
                if other is not None:
                    wrong.append(f"{label}.{name}: title property is named {other!r}")
                    continue
            to_patch[name] = spec
        elif have["type"] != kind:
            wrong.append(f"{label}.{name}: expected {kind}, found {have['type']}")
        elif kind == "relation":
            state = _relation_state(have, spec)
            if state == "upgrade":
                to_patch[name] = spec
            elif state == "wrong":
                wrong.append(f"{label}.{name}: relation target or reverse name differs")
    if wrong:
        detail = "; ".join(wrong)
        # Startup failure: the bot exits, so nothing is posted (the logger has it).
        logger.error("Notion schema check failed: %s", detail)
        raise NotionSchemaError(detail)
    # The API rejects formulas/rollups that read properties added in the same
    # request, so patch plain properties, then rollups, then formulas.
    for group in ("plain", "rollup", "formula"):
        batch = {
            n: sp for n, sp in to_patch.items()
            if (_kind(sp) if _kind(sp) in ("rollup", "formula") else "plain") == group
        }
        if batch:
            logger.info("Adding Notion properties to %s: %s", label, ", ".join(batch))
            ds = await api.request(
                "PATCH", f"/data_sources/{ds_id}", {"properties": batch},
                f"add properties to {label}",
            )
    return ds


def _is_trashed(ds: dict[str, Any]) -> bool:
    return bool(ds.get("in_trash") or ds.get("is_archived") or ds.get("archived"))


async def _create(
    api: NotionApi,
    db: asyncpg.Pool,
    guild_id: int,
    parent_page_id: str,
    title: str,
    props: dict[str, Any],
    ds_key: str,
    db_key: str,
) -> str:
    """Create a database and persist its ids immediately (crash safety)."""
    created = await api.request(
        "POST",
        "/databases",
        {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": props},
        },
        f"create {title} database",
    )
    ds_id = created["data_sources"][0]["id"]
    await set_value(db, guild_id, db_key, created["id"])
    await set_value(db, guild_id, ds_key, ds_id)
    return ds_id


async def _check(api: NotionApi, ds_id: str, label: str) -> str | None:
    """Return None if the data source is usable, else the reason it is not."""
    try:
        ds = await api.request(
            "GET", f"/data_sources/{ds_id}", None, f"verify {label} data source",
            tolerate=(404,),
        )
    except NotionWriteError as exc:
        if exc.status_code != 404:
            raise
        return "returned 404"
    return "is trashed" if _is_trashed(ds) else None


async def ensure_schema(
    api: NotionApi, db: asyncpg.Pool, guild_id: int, parent_page_id: str
) -> NotionIds:
    labels = {"events": "Events", "djs": "DJs", "slots": "Slots"}
    stored: dict[str, str | None] = {
        n: (await get_value(db, guild_id, _KEYS[f"{n}_ds"])) or None for n in labels
    }
    complete = await get_value(db, guild_id, _COMPLETE_KEY) == "1"

    # If any stored database is gone, abandon the whole set and start a fresh one.
    problems = []
    for name, ds_id in stored.items():
        if ds_id is None:
            continue
        reason = await _check(api, ds_id, labels[name])
        if reason:
            problems.append(f"{labels[name]} data source {ds_id} {reason}")
    if complete and any(v is None for v in stored.values()):
        problems.append("a stored database id is missing from the config")
    if problems:
        detail = "; ".join(problems)
        logger.warning("Notion schema: %s; creating a fresh set of databases", detail)
        await api.log.log_attention(
            texts.LOG_FAILED_TITLE,
            "The stored Notion databases did not match what the bot expects, "
            "so it created a new set of Events, Slots and DJs databases.",
            "Old data stays in the old databases and is not copied over. "
            "Archive them in Notion if you no longer need them.",
        )
        await set_value(db, guild_id, _COMPLETE_KEY, "0")
        await set_value(db, guild_id, VIEWS_MARKER_KEY, "0")
        for key in _KEYS.values():
            await set_value(db, guild_id, key, "")
        # Stored DJ page ids point into the abandoned DJs database.
        await clear_all_notion_dj_page_ids(db)
        stored = {n: None for n in labels}
    elif not complete:
        await set_value(db, guild_id, _COMPLETE_KEY, "0")

    # Events and DJs first, Slots needs both data source ids.
    def mk(n: str, title: str, props: dict[str, Any]) -> Any:
        return _create(
            api, db, guild_id, parent_page_id, title, props,
            _KEYS[f"{n}_ds"], _KEYS[f"{n}_db"],
        )

    events_ds = stored["events"] or await mk("events", "Events", _events_props())
    djs_ds = stored["djs"] or await mk("djs", "DJs", _djs_props())
    slots_ds = stored["slots"] or await mk(
        "slots", "Slots", _slots_props(events_ds, djs_ds, dj_dual=False)
    )

    events = await _reconcile(api, events_ds, "Events", _events_props())
    djs = await _reconcile(api, djs_ds, "DJs", _djs_props())
    await _reconcile(api, slots_ds, "Slots", _slots_props(events_ds, djs_ds, dj_dual=True))
    await _reconcile(api, slots_ds, "Slots", _slots_rollups())
    await _reconcile(api, slots_ds, "Slots", _slots_formulas())
    await _reconcile(api, djs_ds, "DJs", _djs_rollups())
    slots = await api.request("GET", f"/data_sources/{slots_ds}", None, "retrieve Slots")

    async def db_id(name: str, ds: dict[str, Any]) -> str:
        value = await get_value(db, guild_id, _KEYS[f"{name}_db"])
        return value or ds["parent"]["database_id"]

    ids = NotionIds(
        events_ds=events_ds,
        slots_ds=slots_ds,
        djs_ds=djs_ds,
        events_db=await db_id("events", events),
        slots_db=await db_id("slots", slots),
        djs_db=await db_id("djs", djs),
        slots_event_property_id=unquote(slots["properties"]["Event"]["id"]),
    )
    await set_value(
        db, guild_id, _KEYS["slots_event_property_id"], ids.slots_event_property_id
    )
    await set_value(db, guild_id, _COMPLETE_KEY, "1")
    return ids
