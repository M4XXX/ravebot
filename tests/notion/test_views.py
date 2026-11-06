"""Tests for bot/notion/views.py — creation of the three Notion views."""
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.schema import NotionIds
from bot.notion.views import ensure_views
from tests.notion.fake_notion import FakeNotion

GUILD = 42
MARKER = (GUILD, "notion_views_created")
EVENT_PROP_ID = "@~pK"


@pytest.fixture
def store() -> Iterator[dict[tuple[int, str], str]]:
    data: dict[tuple[int, str], str] = {}

    async def get(_pool: Any, guild_id: int, key: str) -> str | None:
        return data.get((guild_id, key))

    async def put(_pool: Any, guild_id: int, key: str, value: str) -> None:
        data[(guild_id, key)] = value

    with (
        patch("bot.notion.views.get_value", new=get),
        patch("bot.notion.views.set_value", new=put),
    ):
        yield data


def setup() -> tuple[FakeNotion, NotionApi, NotionIds]:
    fake = FakeNotion()
    api = NotionApi("secret", MagicMock(post_error=AsyncMock()), transport=fake.transport)
    ids: dict[str, str] = {}
    for name in ("Events", "Slots", "DJs"):
        db_id = fake._id()
        ds = fake._new_data_source(db_id, name, {"Name": {"title": {}}})
        fake.databases[db_id] = {"id": db_id}
        view_id = fake._id()
        fake.views[view_id] = {
            "object": "view", "id": view_id, "name": "Default view", "type": "table",
            "database_id": db_id, "data_source_id": ds["id"],
        }
        ids[name] = db_id
        ids[name + "_ds"] = ds["id"]
    return fake, api, NotionIds(
        events_ds=ids["Events_ds"], slots_ds=ids["Slots_ds"], djs_ds=ids["DJs_ds"],
        events_db=ids["Events"], slots_db=ids["Slots"], djs_db=ids["DJs"],
        slots_event_property_id=EVENT_PROP_ID,
    )


def posts(fake: FakeNotion) -> list[dict[str, Any]]:
    return [r[2] for r in fake.requests_to("POST", "/views")]


def by_name(fake: FakeNotion, name: str) -> dict[str, Any]:
    return next(b for b in posts(fake) if b["name"] == name)


async def test_first_run_posts_three_views(store: dict) -> None:
    fake, api, ids = setup()
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    assert sorted(b["name"] for b in posts(fake)) == ["All DJs", "Lineup", "Overview"]
    assert store[MARKER] == "1"

    ov = by_name(fake, "Overview")
    assert ov == {
        "name": "Overview", "type": "table",
        "sorts": [{"property": "Date", "direction": "descending"}],
        "database_id": ids.events_db, "data_source_id": ids.events_ds,
    }
    dj = by_name(fake, "All DJs")
    assert dj["sorts"] == [{"property": "Last booked", "direction": "descending"}]
    assert [p["property_id"] for p in dj["configuration"]["properties"]] == [
        "DJ name", "Discord name", "VRChat name", "Link", "Alternative link",
        "Events booked", "Last booked",
    ]
    assert dj["database_id"] == ids.djs_db and dj["data_source_id"] == ids.djs_ds
    assert all(p["visible"] for p in dj["configuration"]["properties"])


async def test_lineup_body(store: dict) -> None:
    fake, api, ids = setup()
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    assert by_name(fake, "Lineup") == {
        "name": "Lineup", "type": "table",
        "sorts": [{"property": "Slot number", "direction": "ascending"}],
        "configuration": {
            "type": "table",
            "group_by": {
                "type": "relation", "property_id": EVENT_PROP_ID,
                "sort": {"type": "descending"},
            },
            "properties": [
                {"property_id": "Slot number", "visible": True},
                {"property_id": "Time", "visible": True},
                {"property_id": "DJ", "visible": True},
            ],
        },
        "database_id": ids.slots_db, "data_source_id": ids.slots_ds,
    }


async def test_marker_short_circuits_without_requests(store: dict) -> None:
    fake, api, ids = setup()
    store[MARKER] = "1"
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    assert fake.requests == []


async def test_cleared_marker_with_views_present_posts_nothing(store: dict) -> None:
    fake, api, ids = setup()
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    del store[MARKER]
    n = len(posts(fake))
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    assert len(posts(fake)) == n == 3
    assert store[MARKER] == "1"


async def test_failed_create_retries_only_missing(store: dict) -> None:
    fake, api, ids = setup()
    fake.fail_once("POST", "/views", 400, skip=1)  # second create fails
    with pytest.raises(NotionWriteError):
        await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    assert MARKER not in store
    first = [b["name"] for b in posts(fake)]
    assert len(first) == 2  # second POST failed but is recorded
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    assert store[MARKER] == "1"
    retried = [b["name"] for b in posts(fake)][2:]
    assert len(retried) == 2 and first[0] not in retried
    assert sorted(set(retried) | {first[0]}) == ["All DJs", "Lineup", "Overview"]


async def test_default_view_untouched(store: dict) -> None:
    fake, api, ids = setup()
    defaults = {k: dict(v) for k, v in fake.views.items()}
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    for k, v in defaults.items():
        assert fake.views[k] == v
    assert all(r[0] in ("GET", "POST") for r in fake.requests)
    assert all(r[0] != "DELETE" and r[0] != "PATCH" for r in fake.requests)


async def test_follows_view_list_pagination(store: dict) -> None:
    fake, api, ids = setup()
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    del store[MARKER]
    fake.views_page_size = 1
    n = len(posts(fake))
    await ensure_views(api, None, GUILD, ids)  # type: ignore[arg-type]
    assert len(posts(fake)) == n
