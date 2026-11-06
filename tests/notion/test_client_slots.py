"""Tests for NotionClient.assign_slot / clear_slot against the fake Notion API."""
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.client import NotionClient, SlotRowMissingError
from bot.notion.schema import NotionIds
from tests.notion.fake_notion import FakeNotion


@pytest.fixture
def env():
    fake = FakeNotion()
    log = MagicMock()
    log.post_error = AsyncMock()
    api = NotionApi("key", log, transport=fake.transport)
    ids = {}
    for name in ("events", "slots", "djs"):
        db = fake._create_database(
            {"title": [{"text": {"content": name}}], "parent": {}, "initial_data_source": {
                "properties": {"Title": {"title": {}}}}}
        )
        ids[name] = db.json()
    nids = NotionIds(
        events_ds=ids["events"]["data_sources"][0]["id"],
        slots_ds=ids["slots"]["data_sources"][0]["id"],
        djs_ds=ids["djs"]["data_sources"][0]["id"],
        events_db=ids["events"]["id"], slots_db=ids["slots"]["id"], djs_db=ids["djs"]["id"],
        slots_event_property_id="x",
    )
    return fake, NotionClient(api, nids)


async def make_event(client):
    return await client.create_event(
        "2026-12-04 Rave Night", datetime.date(2026, 12, 4), datetime.time(20, 0), 3, 60
    )


def set_dj(fake, page_id, *dj_ids):
    fake.pages[page_id]["properties"]["DJ"] = {"relation": [{"id": i} for i in dj_ids]}


def dj_of(fake, page_id):
    return [r["id"] for r in fake.pages[page_id]["properties"].get("DJ", {}).get("relation", [])]


def patches(fake):
    return [r for r in fake.requests if r[0] == "PATCH"]


@pytest.mark.asyncio
async def test_assign_empty_slot_writes_relation(env):
    fake, client = env
    event = await make_event(client)
    assert await client.assign_slot(event, 1, "dj-1") is True
    assert dj_of(fake, event.slots[1].page_id) == ["dj-1"]
    assert patches(fake)[0][2] == {"properties": {"DJ": {"relation": [{"id": "dj-1"}]}}}


@pytest.mark.asyncio
async def test_assign_filled_slot_writes_nothing(env):
    fake, client = env
    event = await make_event(client)
    set_dj(fake, event.slots[0].page_id, "other")
    assert await client.assign_slot(event, 0, "dj-1") is False
    assert dj_of(fake, event.slots[0].page_id) == ["other"]
    assert patches(fake) == []


@pytest.mark.asyncio
async def test_assign_slot_already_held_by_same_dj_returns_false(env):
    fake, client = env
    event = await make_event(client)
    set_dj(fake, event.slots[0].page_id, "dj-1")
    assert await client.assign_slot(event, 0, "dj-1") is False
    assert patches(fake) == []


@pytest.mark.asyncio
async def test_same_dj_can_take_two_slots_of_one_event(env):
    fake, client = env
    event = await make_event(client)
    assert await client.assign_slot(event, 0, "dj-1") is True
    assert await client.assign_slot(event, 2, "dj-1") is True
    assert dj_of(fake, event.slots[0].page_id) == ["dj-1"]
    assert dj_of(fake, event.slots[2].page_id) == ["dj-1"]


@pytest.mark.asyncio
async def test_assign_rereads_page_so_stale_event_cannot_overwrite(env):
    fake, client = env
    event = await make_event(client)  # shows slot 1 open
    assert event.slots[0].dj_page_id is None
    set_dj(fake, event.slots[0].page_id, "someone-else")  # booked meanwhile
    assert await client.assign_slot(event, 0, "dj-1") is False
    assert dj_of(fake, event.slots[0].page_id) == ["someone-else"]
    assert patches(fake) == []


@pytest.mark.asyncio
async def test_assign_missing_row_raises(env):
    _, client = env
    event = await make_event(client)
    with pytest.raises(SlotRowMissingError):
        await client.assign_slot(event, 3, "dj-1")


@pytest.mark.asyncio
async def test_assign_write_failure_raises_notion_write_error(env):
    fake, client = env
    event = await make_event(client)
    fake.fail_once("PATCH", "/pages/", 400)
    with pytest.raises(NotionWriteError):
        await client.assign_slot(event, 0, "dj-1")


@pytest.mark.asyncio
async def test_clear_removes_only_named_dj(env):
    fake, client = env
    event = await make_event(client)
    set_dj(fake, event.slots[0].page_id, "dj-1")
    assert await client.clear_slot(event, 0, "dj-1") is True
    assert dj_of(fake, event.slots[0].page_id) == []
    set_dj(fake, event.slots[1].page_id, "dj-1", "dj-2")
    assert await client.clear_slot(event, 1, "dj-1") is True
    assert dj_of(fake, event.slots[1].page_id) == ["dj-2"]


@pytest.mark.asyncio
async def test_clear_slot_held_by_other_dj_returns_false(env):
    fake, client = env
    event = await make_event(client)
    set_dj(fake, event.slots[0].page_id, "dj-2")
    assert await client.clear_slot(event, 0, "dj-1") is False
    assert dj_of(fake, event.slots[0].page_id) == ["dj-2"]
    assert patches(fake) == []


@pytest.mark.asyncio
async def test_clear_empty_slot_returns_true_without_writing(env):
    fake, client = env
    event = await make_event(client)
    assert await client.clear_slot(event, 0, "dj-1") is True
    assert patches(fake) == []


@pytest.mark.asyncio
async def test_clear_missing_row_raises(env):
    _, client = env
    event = await make_event(client)
    with pytest.raises(SlotRowMissingError):
        await client.clear_slot(event, 7, "dj-1")


@pytest.mark.asyncio
async def test_force_assign_replaces_held_slot_and_extra_djs(env):
    fake, client = env
    event = await make_event(client)
    set_dj(fake, event.slots[1].page_id, "old-dj", "extra-dj")
    await client.force_assign_slot(event, 1, "dj-1")
    assert dj_of(fake, event.slots[1].page_id) == ["dj-1"]
    assert patches(fake)[-1][2] == {"properties": {"DJ": {"relation": [{"id": "dj-1"}]}}}


@pytest.mark.asyncio
async def test_force_assign_empty_slot(env):
    fake, client = env
    event = await make_event(client)
    await client.force_assign_slot(event, 0, "dj-1")
    assert dj_of(fake, event.slots[0].page_id) == ["dj-1"]


@pytest.mark.asyncio
async def test_force_assign_missing_row_raises(env):
    fake, client = env
    event = await make_event(client)
    with pytest.raises(SlotRowMissingError):
        await client.force_assign_slot(event, 3, "dj-1")
    assert patches(fake) == []


@pytest.mark.asyncio
async def test_create_slot_rows_creates_exactly_the_given_numbers(env):
    fake, client = env
    event = await make_event(client)
    before = len(fake.requests_to("POST", "/pages"))
    rows = await client.create_slot_rows(event, [4, 5])
    assert len(fake.requests_to("POST", "/pages")) == before + 2
    assert [r.number for r in rows] == [4, 5]
    assert all(r.dj_page_id is None and r.extra_dj_page_ids == () for r in rows)
    page = fake.pages[rows[0].page_id]
    props = page["properties"]
    assert props["Name"]["title"][0]["plain_text"] == "2026-12-04 Rave Night #4"
    assert props["Event"]["relation"] == [{"id": event.page_id}]
    assert props["Slot number"]["number"] == 4
    assert "DJ" not in props
    assert page["parent"]["data_source_id"] == client._ids.slots_ds


@pytest.mark.asyncio
async def test_create_slot_rows_empty_list_sends_nothing(env):
    fake, client = env
    event = await make_event(client)
    before = len(fake.requests)
    assert await client.create_slot_rows(event, []) == []
    assert len(fake.requests) == before


@pytest.mark.asyncio
async def test_create_slot_rows_failure_raises_notion_write_error(env):
    fake, client = env
    event = await make_event(client)
    fake.fail_once("POST", "/pages")
    with pytest.raises(NotionWriteError):
        await client.create_slot_rows(event, [4])


@pytest.mark.asyncio
async def test_trash_slot_rows_patches_in_trash(env):
    fake, client = env
    event = await make_event(client)
    await client.trash_slot_rows([event.slots[1], event.slots[2]])
    assert patches(fake) == [
        ("PATCH", f"/pages/{event.slots[1].page_id}", {"in_trash": True}),
        ("PATCH", f"/pages/{event.slots[2].page_id}", {"in_trash": True}),
    ]
    assert fake.pages[event.slots[1].page_id]["in_trash"] is True
    assert fake.pages[event.slots[0].page_id]["in_trash"] is False
    listed = await client.list_events(include_past=True)
    assert [s.number for s in listed[0].slots] == [1]


@pytest.mark.asyncio
async def test_trash_slot_rows_failure_raises_notion_write_error(env):
    fake, client = env
    event = await make_event(client)
    fake.fail_once("PATCH", "/pages/")
    with pytest.raises(NotionWriteError):
        await client.trash_slot_rows([event.slots[0]])


@pytest.mark.asyncio
async def test_keep_first_dj_removes_extra_djs(env):
    fake, client = env
    event = await make_event(client)
    set_dj(fake, event.slots[1].page_id, "dj-1", "dj-2", "dj-3")
    await client.keep_first_dj(event, 1)
    assert dj_of(fake, event.slots[1].page_id) == ["dj-1"]
    assert patches(fake) == [
        (
            "PATCH",
            f"/pages/{event.slots[1].page_id}",
            {"properties": {"DJ": {"relation": [{"id": "dj-1"}]}}},
        )
    ]


@pytest.mark.asyncio
async def test_keep_first_dj_rereads_the_row(env):
    fake, client = env
    event = await make_event(client)  # stale: no DJs known
    set_dj(fake, event.slots[0].page_id, "dj-a", "dj-b")
    await client.keep_first_dj(event, 0)
    assert dj_of(fake, event.slots[0].page_id) == ["dj-a"]


@pytest.mark.asyncio
async def test_keep_first_dj_with_one_or_no_dj_writes_nothing(env):
    fake, client = env
    event = await make_event(client)
    set_dj(fake, event.slots[0].page_id, "dj-1")
    await client.keep_first_dj(event, 0)
    await client.keep_first_dj(event, 1)
    assert patches(fake) == []
    assert dj_of(fake, event.slots[0].page_id) == ["dj-1"]


@pytest.mark.asyncio
async def test_keep_first_dj_missing_row_raises(env):
    fake, client = env
    event = await make_event(client)
    with pytest.raises(SlotRowMissingError):
        await client.keep_first_dj(event, 3)
    assert patches(fake) == []
