"""Tests for bot/notion/schema.py — database creation and verification."""
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import pytest

from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.schema import (
    SLOT_STATUS_FORMULA,
    TIME_FORMULA,
    NotionIds,
    NotionSchemaError,
    ensure_schema,
)
from bot.ui.embeds import ErrorCode
from tests.notion.fake_notion import FakeNotion

GUILD = 42
PARENT = "parent-page"

EVENT_PROPS = {
    "ID": "title", "Date": "date", "Status": "select", "Genre": "rich_text",
    "Notes": "rich_text", "Slots": "number", "Start": "rich_text",
    "Slot length": "number", "Slot rows": "relation",
}
SLOT_PROPS = {
    "Name": "title", "Event": "relation", "Slot number": "number", "DJ": "relation",
    "Event start": "rollup", "Event slot length": "rollup", "Event date src": "rollup",
    "Event date": "formula", "Time": "formula", "Slot status": "formula",
}
DJ_PROPS = {
    "DJ name": "title", "Discord name": "rich_text", "Discord ID": "rich_text",
    "VRChat name": "rich_text", "Logo": "files", "Link": "url",
    "Alternative link": "url", "Events booked": "rollup", "Last booked": "rollup",
    "Slots": "relation",
}


@pytest.fixture
def store() -> Iterator[dict[tuple[int, str], str]]:
    data: dict[tuple[int, str], str] = {}

    async def get(_pool: Any, guild_id: int, key: str) -> str | None:
        return data.get((guild_id, key))

    async def put(_pool: Any, guild_id: int, key: str, value: str) -> None:
        data[(guild_id, key)] = value

    with (
        patch("bot.notion.schema.get_value", new=get),
        patch("bot.notion.schema.set_value", new=put),
    ):
        yield data


def make(fake: FakeNotion) -> tuple[NotionApi, MagicMock]:
    log = MagicMock()
    log.post_error = AsyncMock(return_value=None)
    return NotionApi("secret", log, transport=fake.transport), log


def types(fake: FakeNotion, title: str) -> dict[str, str]:
    return {n: p["type"] for n, p in fake.ds_by_title(title)["properties"].items()}


async def test_first_run_creates_everything(store: dict) -> None:
    fake = FakeNotion()
    api, _ = make(fake)
    ids = await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]

    assert len(fake.databases) == 3
    assert types(fake, "Events") == EVENT_PROPS
    assert types(fake, "Slots") == SLOT_PROPS
    assert types(fake, "DJs") == DJ_PROPS

    ev, sl, dj = (fake.ds_by_title(t) for t in ("Events", "Slots", "DJs"))
    assert (ids.events_ds, ids.slots_ds, ids.djs_ds) == (ev["id"], sl["id"], dj["id"])
    assert ids.events_db == ev["parent"]["database_id"]
    assert ids.slots_db == sl["parent"]["database_id"]
    assert ids.djs_db == dj["parent"]["database_id"]
    # relations are dual with the agreed names
    rel = sl["properties"]["Event"]["relation"]
    assert rel["type"] == "dual_property"
    assert rel["dual_property"]["synced_property_name"] == "Slot rows"
    rel = sl["properties"]["DJ"]["relation"]
    assert rel["type"] == "dual_property"
    assert rel["dual_property"]["synced_property_name"] == "Slots"
    assert ev["properties"]["Slot rows"]["relation"]["data_source_id"] == sl["id"]
    assert dj["properties"]["Slots"]["relation"]["data_source_id"] == sl["id"]
    # status options, formulas, rollups
    opts = [o["name"] for o in ev["properties"]["Status"]["select"]["options"]]
    assert opts == ["Planning not started", "Planning stage", "Ready", "Done", "Canceled"]
    p = sl["properties"]
    assert p["Slot status"]["formula"]["expression"] == SLOT_STATUS_FORMULA
    assert SLOT_STATUS_FORMULA == 'if(empty(prop("DJ")), "Open", "Booked")'
    assert p["Time"]["formula"]["expression"] == TIME_FORMULA
    assert TIME_FORMULA == (
        'lets(m, mod(toNumber(prop("Event start").substring(0, 2)) * 60 + '
        'toNumber(prop("Event start").substring(3, 5)) + (prop("Slot number") - 1) * '
        'prop("Event slot length"), 1440), format(floor(m / 60)).padStart(2, "0") + ":" '
        '+ format(m % 60).padStart(2, "0"))'
    )
    for name, rel, target, fn in [
        ("Event start", "Event", "Start", "show_original"),
        ("Event slot length", "Event", "Slot length", "max"),
        ("Event date src", "Event", "Date", "latest_date"),
    ]:
        assert p[name]["rollup"] == {
            "relation_property_name": rel, "rollup_property_name": target, "function": fn,
        }
    assert p["Event date"]["formula"]["expression"] == 'prop("Event date src")'
    for name, target, fn in [
        ("Events booked", "Event", "unique"), ("Last booked", "Event date", "latest_date"),
    ]:
        assert dj["properties"][name]["rollup"] == {
            "relation_property_name": "Slots", "rollup_property_name": target,
            "function": fn,
        }
    assert dj["properties"]["Events booked"]["rollup"]["function"] == "unique"
    assert dj["properties"]["Last booked"]["rollup"]["function"] == "latest_date"
    # event relation property id is URL-decoded
    assert ids.slots_event_property_id == unquote(sl["properties"]["Event"]["id"])
    # config keys stored
    keys = {k for (_, k) in store}
    assert keys == {
        "notion_events_ds", "notion_slots_ds", "notion_djs_ds",
        "notion_events_db", "notion_slots_db", "notion_djs_db", "notion_slots_event_prop",
        "notion_schema_complete",
    }
    assert isinstance(ids, NotionIds)
    assert store[(GUILD, "notion_schema_complete")] == "1"
    assert store[(GUILD, "notion_events_db")] == ids.events_db


async def test_second_run_creates_nothing(store: dict) -> None:
    fake = FakeNotion()
    api, log = make(fake)
    first = await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]
    fake.requests.clear()
    second = await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]

    assert second == first
    assert len(fake.databases) == 3
    assert not fake.requests_to("POST", "/databases")
    assert not fake.requests_to("PATCH", "/data_sources")
    log.post_error.assert_not_awaited()


async def _run(api: NotionApi) -> NotionIds:
    return await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["404", "trashed"])
async def test_missing_database_creates_fresh_set_of_all_three(
    store: dict, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    fake = FakeNotion()
    api, log = make(fake)
    first = await _run(api)
    if mode == "404":
        fake.trash_data_source(first.djs_ds)
    else:
        fake.mark_trashed(first.djs_ds)

    with caplog.at_level(logging.WARNING):
        second = await _run(api)

    assert len(fake.databases) == 6
    assert {second.events_ds, second.slots_ds, second.djs_ds}.isdisjoint(
        {first.events_ds, first.slots_ds, first.djs_ds}
    )
    assert store[(GUILD, "notion_djs_ds")] == second.djs_ds
    assert store[(GUILD, "notion_schema_complete")] == "1"
    assert any("fresh set" in r.message for r in caplog.records)
    log.post_error.assert_awaited_once()
    trigger = log.post_error.await_args.kwargs["trigger"]
    assert "DJs" in trigger and "Events" not in trigger
    assert "old" in log.post_error.await_args.kwargs["manual_action"].lower()
    # old data source untouched, new set is wired to the new set
    sl = fake.data_sources[second.slots_ds]
    assert sl["properties"]["DJ"]["relation"]["data_source_id"] == second.djs_ds
    assert sl["properties"]["Event"]["relation"]["data_source_id"] == second.events_ds


@pytest.mark.parametrize(
    ("method", "prefix", "skip", "expected_dbs"),
    [
        ("POST", "/databases", 2, 2),  # Slots creation fails
        ("POST", "/databases", 1, 1),  # DJs creation fails
        ("PATCH", "/data_sources/", 0, 3),  # relation PATCH fails
        ("PATCH", "/data_sources/", 1, 3),  # helper/formula PATCH fails
    ],
)
async def test_partial_failure_then_rerun_resumes_without_duplicates(
    store: dict, method: str, prefix: str, skip: int, expected_dbs: int
) -> None:
    fake = FakeNotion()
    api, log = make(fake)
    fake.fail_once(method, prefix, skip=skip)
    with pytest.raises(NotionWriteError):
        await _run(api)
    assert len(fake.databases) == expected_dbs
    assert store.get((GUILD, "notion_schema_complete")) != "1"
    log.post_error.reset_mock()

    ids = await _run(api)

    assert len(fake.databases) == 3
    assert types(fake, "Events") == EVENT_PROPS
    assert types(fake, "Slots") == SLOT_PROPS
    assert types(fake, "DJs") == DJ_PROPS
    assert store[(GUILD, "notion_schema_complete")] == "1"
    assert store[(GUILD, "notion_slots_ds")] == ids.slots_ds
    log.post_error.assert_not_awaited()  # resuming is not an error


async def test_missing_property_is_added(store: dict) -> None:
    fake = FakeNotion()
    api, log = make(fake)
    await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]
    del fake.ds_by_title("Events")["properties"]["Genre"]
    del fake.ds_by_title("DJs")["properties"]["Last booked"]

    await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]

    assert types(fake, "Events") == EVENT_PROPS
    assert types(fake, "DJs") == DJ_PROPS
    assert len(fake.databases) == 3
    log.post_error.assert_not_awaited()


async def test_wrong_property_type_is_reported_and_raises(store: dict) -> None:
    fake = FakeNotion()
    api, log = make(fake)
    await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]
    ev = fake.ds_by_title("Events")
    ev["properties"]["Genre"]["type"] = "number"

    with pytest.raises(NotionSchemaError, match="Genre"):
        await ensure_schema(api, object(), GUILD, PARENT)  # type: ignore[arg-type]

    log.post_error.assert_awaited()
    kwargs = log.post_error.await_args.kwargs
    assert kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE
    assert "Genre" in kwargs["trigger"]
