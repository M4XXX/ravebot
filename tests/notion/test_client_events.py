"""Tests for NotionClient event/slot create and read against the fake Notion."""
import datetime
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.notion.api import NotionApi
from bot.notion.client import DuplicateEventError, NotionClient, NotionWriteError
from bot.notion.schema import NotionIds
from tests.notion.fake_notion import FakeNotion

FUTURE = datetime.date(2099, 1, 10)
PAST = datetime.date(2000, 1, 10)


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
    return fake, NotionClient(api, nids), nids


def pages_in(fake, ds):
    return [p for p in fake.pages.values() if p["parent"]["data_source_id"] == ds]


def add_dj(fake, nids, name, discord_id="123"):
    r = fake._create_page({"parent": {"type": "data_source_id", "data_source_id": nids.djs_ds},
        "properties": {"DJ name": {"title": [{"text": {"content": name}}]},
                       "Discord ID": {"rich_text": [{"text": {"content": discord_id}}]}}})
    return r.json()["id"]


async def test_create_event_writes_event_and_numbered_slots(env):
    fake, client, nids = env
    ev = await client.create_event(
        "2099-01-10 Rave", FUTURE, datetime.time(18, 0), 3, 60, genre="Techno"
    )
    events = pages_in(fake, nids.events_ds)
    slots = pages_in(fake, nids.slots_ds)
    assert len(events) == 1 and len(slots) == 3
    props = events[0]["properties"]
    assert props["Status"] == {"select": {"name": "Planning not started"}}
    assert props["Date"] == {"date": {"start": "2099-01-10"}}
    assert props["Genre"]["rich_text"][0]["text"]["content"] == "Techno"
    assert props["Start"]["rich_text"][0]["text"]["content"] == "18:00"
    assert props["Slots"] == {"number": 3} and props["Slot length"] == {"number": 60}
    assert events[0]["parent"] == {"type": "data_source_id", "data_source_id": nids.events_ds}
    assert sorted(s["properties"]["Slot number"]["number"] for s in slots) == [1, 2, 3]
    for s in slots:
        assert "DJ" not in s["properties"]
        assert s["properties"]["Event"] == {"relation": [{"id": events[0]["id"]}]}
        n = s["properties"]["Slot number"]["number"]
        assert s["properties"]["Name"]["title"][0]["text"]["content"] == f"2099-01-10 Rave #{n}"
    assert ev.page_id == events[0]["id"] and [s.number for s in ev.slots] == [1, 2, 3]
    assert (await client.list_events())[0].slots == ev.slots


async def test_create_event_omits_genre_when_absent(env):
    fake, client, nids = env
    await client.create_event("E1", FUTURE, datetime.time(18, 0), 1, 60)
    assert "Genre" not in pages_in(fake, nids.events_ds)[0]["properties"]


async def test_duplicate_id_raises_and_writes_nothing(env):
    fake, client, nids = env
    await client.create_event("E1", FUTURE, datetime.time(18, 0), 2, 60)
    before = len(fake.requests_to("POST", "/pages"))
    with pytest.raises(DuplicateEventError):
        await client.create_event("E1", FUTURE, datetime.time(18, 0), 2, 60)
    assert len(fake.requests_to("POST", "/pages")) == before
    assert await client.event_id_exists("E1") and not await client.event_id_exists("E2")


async def test_trashed_event_does_not_count_as_duplicate(env):
    fake, client, nids = env
    await client.create_event("E1", FUTURE, datetime.time(18, 0), 1, 60)
    pages_in(fake, nids.events_ds)[0]["is_archived"] = True
    assert not await client.event_id_exists("E1")


@pytest.mark.parametrize("bad", ["", "   "])
async def test_empty_id_raises_value_error(env, bad):
    _, client, _ = env
    with pytest.raises(ValueError):
        await client.create_event(bad, FUTURE, datetime.time(18, 0), 1, 60)


async def test_slot_failure_propagates_and_event_stays(env):
    fake, client, nids = env
    fake.fail_once("POST", "/pages", 400, skip=2)  # event ok, slot 1 ok, slot 2 fails
    with pytest.raises(NotionWriteError):
        await client.create_event("E1", FUTURE, datetime.time(18, 0), 3, 60)
    assert len(pages_in(fake, nids.events_ds)) == 1
    assert len(pages_in(fake, nids.slots_ds)) == 1


async def test_list_events_joins_slots_and_djs(env):
    fake, client, nids = env
    ev = await client.create_event("E1", FUTURE, datetime.time(18, 0), 2, 60)
    dj = add_dj(fake, nids, "DJ Test", "42")
    fake.pages[ev.slots[1].page_id]["properties"]["DJ"] = {"relation": [{"id": dj}]}
    [got] = await client.list_events()
    assert got.slots[0].dj_name is None
    assert (got.slots[1].dj_name, got.slots[1].discord_id) == ("DJ Test", 42)


async def test_dj_with_two_slots_in_one_event(env):
    fake, client, nids = env
    ev = await client.create_event("E1", FUTURE, datetime.time(18, 0), 2, 60)
    dj = add_dj(fake, nids, "DJ Test")
    for s in ev.slots:
        fake.pages[s.page_id]["properties"]["DJ"] = {"relation": [{"id": dj}]}
    [got] = await client.list_events()
    assert [s.dj_name for s in got.slots] == ["DJ Test", "DJ Test"]


async def test_second_dj_on_slot_in_extra_ids(env):
    fake, client, nids = env
    ev = await client.create_event("E1", FUTURE, datetime.time(18, 0), 1, 60)
    d1, d2 = add_dj(fake, nids, "A"), add_dj(fake, nids, "B")
    fake.pages[ev.slots[0].page_id]["properties"]["DJ"] = {"relation": [{"id": d1}, {"id": d2}]}
    [got] = await client.list_events()
    assert got.slots[0].dj_page_id == d1 and got.slots[0].extra_dj_page_ids == (d2,)


async def test_archived_rows_skipped(env):
    fake, client, nids = env
    ev = await client.create_event("E1", FUTURE, datetime.time(18, 0), 2, 60)
    await client.create_event("E2", FUTURE, datetime.time(19, 0), 1, 60)
    dj = add_dj(fake, nids, "Gone")
    fake.pages[dj]["in_trash"] = True
    fake.pages[ev.slots[1].page_id]["properties"]["DJ"] = {"relation": [{"id": dj}]}
    fake.pages[ev.slots[0].page_id]["is_archived"] = True
    fake.pages[pages_in(fake, nids.events_ds)[1]["id"]]["in_trash"] = True
    events = await client.list_events()
    assert [e.event_id for e in events] == ["E1"]
    assert [s.number for s in events[0].slots] == [2]
    assert events[0].slots[0].dj_name is None


async def test_invalid_event_skipped_and_logged_once(env, caplog):
    fake, client, nids = env
    await client.create_event("E1", FUTURE, datetime.time(18, 0), 1, 60)
    fake._create_page({"parent": {"type": "data_source_id", "data_source_id": nids.events_ds},
                       "properties": {"ID": {"title": [{"text": {"content": "Bad"}}]}}})
    with caplog.at_level(logging.WARNING):
        events = await client.list_events()
    assert [e.event_id for e in events] == ["E1"]
    assert sum("Skipping event page" in r.message for r in caplog.records) == 1


async def test_slot_number_zero_and_orphans_skipped(env, caplog):
    fake, client, nids = env
    ev = await client.create_event("E1", FUTURE, datetime.time(18, 0), 1, 60)
    slot_parent = {"type": "data_source_id", "data_source_id": nids.slots_ds}
    fake._create_page({"parent": slot_parent, "properties": {
        "Event": {"relation": [{"id": ev.page_id}]}, "Slot number": {"number": 0}}})
    fake._create_page({"parent": slot_parent, "properties": {
        "Event": {"relation": []}, "Slot number": {"number": 5}}})
    with caplog.at_level(logging.WARNING):
        [got] = await client.list_events()
    assert [s.number for s in got.slots] == [1]
    assert sum("slot number" in r.message for r in caplog.records) == 1


async def test_include_past_and_sorting(env):
    _, client, _ = env
    await client.create_event("Late", FUTURE, datetime.time(20, 0), 1, 60)
    await client.create_event("Early", FUTURE, datetime.time(18, 0), 1, 60)
    await client.create_event("Old", PAST, datetime.time(18, 0), 1, 60)
    assert [e.event_id for e in await client.list_events()] == ["Early", "Late"]
    assert [e.event_id for e in await client.list_events(include_past=True)] == [
        "Old", "Early", "Late"]


def seed_events(fake, nids, count):
    parent = {"type": "data_source_id", "data_source_id": nids.events_ds}
    ids = []
    for i in range(count):
        r = fake._create_page({"parent": parent, "properties": {
            "ID": {"title": [{"text": {"content": f"E{i:03d}"}}]},
            "Date": {"date": {"start": "2099-01-10"}},
            "Slots": {"number": 1}, "Slot length": {"number": 60},
            "Start": {"rich_text": [{"text": {"content": "18:00"}}]}}})
        ids.append(r.json()["id"])
    return ids


async def test_list_events_paginates_events_and_slots(env):
    fake, client, nids = env
    event_ids = seed_events(fake, nids, 101)
    slot_parent = {"type": "data_source_id", "data_source_id": nids.slots_ds}
    for eid in event_ids:
        fake._create_page({"parent": slot_parent, "properties": {
            "Event": {"relation": [{"id": eid}]}, "Slot number": {"number": 1}}})
    fake._create_page({"parent": slot_parent, "properties": {
        "Event": {"relation": [{"id": event_ids[-1]}]}, "Slot number": {"number": 2}}})
    events = await client.list_events()
    assert len(events) == 101
    assert len(fake.requests_to("POST", f"/data_sources/{nids.events_ds}/query")) == 2
    assert len(fake.requests_to("POST", f"/data_sources/{nids.slots_ds}/query")) == 2
    assert sum(len(e.slots) for e in events) == 102
    assert [s.number for s in events[-1].slots] == [1, 2]


async def test_duplicate_check_when_match_not_on_first_page(env):
    fake, client, nids = env
    seed_events(fake, nids, 101)
    fake.ignore_query_filters = True  # server returns every row, match is on page 2
    assert await client.event_id_exists("E100")
    assert not await client.event_id_exists("E999")
    before = len(fake.requests_to("POST", "/pages"))
    with pytest.raises(DuplicateEventError):
        await client.create_event("E100", FUTURE, datetime.time(18, 0), 1, 60)
    assert len(fake.requests_to("POST", "/pages")) == before
    await client.create_event("E999", FUTURE, datetime.time(18, 0), 1, 60)


@pytest.mark.parametrize("count,minutes", [(0, 60), (-1, 60), (1, 0), (1, -5)])
async def test_invalid_sizes_raise_before_any_request(env, count, minutes):
    fake, client, _ = env
    n = len(fake.requests)
    with pytest.raises(ValueError):
        await client.create_event("E1", FUTURE, datetime.time(18, 0), count, minutes)
    assert len(fake.requests) == n


async def test_restore_event_shape_patches_start_and_slot_length_only(env):
    fake, client, nids = env
    ev = await client.create_event("2099-01-10 Rave", FUTURE, datetime.time(18, 0), 2, 60)
    fake.pages[ev.page_id]["properties"]["Start"] = {
        "rich_text": [{"text": {"content": "21:15"}, "plain_text": "21:15"}]}
    fake.pages[ev.page_id]["properties"]["Slot length"] = {"number": 45}
    n = len(fake.requests)
    await client.restore_event_shape(ev, datetime.time(18, 0), 60)
    reqs = fake.requests[n:]
    assert len(reqs) == 1
    method, path, body = reqs[0]
    assert (method, path) == ("PATCH", f"/pages/{ev.page_id}")
    assert set(body["properties"]) == {"Start", "Slot length"}
    props = fake.pages[ev.page_id]["properties"]
    assert props["Start"]["rich_text"][0]["plain_text"] == "18:00"
    assert props["Slot length"] == {"number": 60}


async def test_restore_event_shape_raises_on_failure(env):
    fake, client, _ = env
    ev = await client.create_event("2099-01-10 Rave", FUTURE, datetime.time(18, 0), 1, 60)
    fake.fail_once("PATCH", f"/pages/{ev.page_id}", 400)
    with pytest.raises(NotionWriteError):
        await client.restore_event_shape(ev, datetime.time(18, 0), 60)


async def test_slot_count_above_100_raises_before_any_request(env):
    fake, client, _ = env
    n = len(fake.requests)
    with pytest.raises(ValueError):
        await client.create_event("E1", FUTURE, datetime.time(18, 0), 101, 60)
    assert len(fake.requests) == n
