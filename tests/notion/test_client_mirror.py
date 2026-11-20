"""Tests for the NotionClient mirror methods and payload round trips."""
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.notion.api import NotionApi
from bot.notion.client import NotionClient
from bot.notion.schema import NotionIds
from bot.sync.payloads import (
    dj_payload,
    event_payload,
    notion_dj_payload,
    notion_event_payload,
    notion_slot_payload,
    payload_hash,
    slot_payload,
)
from tests.notion.fake_notion import FakeNotion


@pytest.fixture
def env():
    fake = FakeNotion()
    log = MagicMock()
    log.post_error = AsyncMock()
    log.log_attention = AsyncMock()
    api = NotionApi("key", log, transport=fake.transport)
    ds = {}
    for name in ("events", "slots", "djs"):
        db = fake._create_database(
            {"title": [{"text": {"content": name}}], "parent": {}, "initial_data_source": {
                "properties": {"Title": {"title": {}}}}}
        ).json()
        ds[name] = db
    nids = NotionIds(
        events_ds=ds["events"]["data_sources"][0]["id"],
        slots_ds=ds["slots"]["data_sources"][0]["id"],
        djs_ds=ds["djs"]["data_sources"][0]["id"],
        events_db=ds["events"]["id"], slots_db=ds["slots"]["id"], djs_db=ds["djs"]["id"],
        slots_event_property_id="x",
    )
    return fake, NotionClient(api, nids), nids


EVENT_ROW = {"event_id": "Friday", "date": datetime.date(2026, 6, 13), "start": "20:00",
             "slot_minutes": 60, "slot_count": 4, "status": "Planned", "genre": "Techno",
             "notes": "bring gear"}
DJ_ROW = {"discord_user_id": 123, "dj_name": "DJ A", "vrchat_name": "vrc",
          "logo_url": "https://x.test/l.png", "stream_url_primary": "https://twitch.tv/a",
          "stream_url_backup": None, "logo_image": None}


async def test_payload_round_trip_event(env):
    fake, client, _ = env
    page_id = await client.mirror_event("uid-1", event_payload(EVENT_ROW), None)
    assert notion_event_payload(fake.pages[page_id]) == event_payload(EVENT_ROW)


async def test_payload_round_trip_event_cleared_fields(env):
    fake, client, _ = env
    row = {**EVENT_ROW, "genre": None, "notes": None, "status": None}
    page_id = await client.mirror_event("uid-1", event_payload(row), None)
    await client.mirror_event("uid-1", event_payload(EVENT_ROW), page_id)
    await client.mirror_event("uid-1", event_payload(row), page_id)
    assert notion_event_payload(fake.pages[page_id]) == event_payload(row)


async def test_payload_round_trip_dj_external_logo(env):
    fake, client, _ = env
    page_id = await client.mirror_dj(
        dj_payload(DJ_ROW), None, logo_image=None, upload_logo=True, logo_url=DJ_ROW["logo_url"])
    got = notion_dj_payload(fake.pages[page_id])
    assert got == dj_payload(DJ_ROW)
    assert payload_hash(got) == payload_hash(dj_payload(DJ_ROW))
    assert "logo_url" not in got
    assert fake.pages[page_id]["properties"]["Logo"]["files"][0]["external"]["url"] == (
        "https://x.test/l.png")


async def test_payload_round_trip_dj_uploaded_logo(env):
    fake, client, _ = env
    row = {**DJ_ROW, "logo_image": b"png"}
    client._api.upload_file = AsyncMock(return_value="up-1")
    page_id = await client.mirror_dj(
        dj_payload(row), None, logo_image=b"png", upload_logo=True, logo_url=row["logo_url"])
    got = notion_dj_payload(fake.pages[page_id])
    assert got == dj_payload(row)
    assert payload_hash(got) == payload_hash(dj_payload(row))
    assert fake.pages[page_id]["properties"]["Logo"]["files"][0]["type"] == "file_upload"


async def test_payload_round_trip_slot(env):
    fake, client, _ = env
    ev = await client.mirror_event("uid-1", event_payload(EVENT_ROW), None)
    dj = await client.mirror_dj(dj_payload(DJ_ROW), None, logo_image=None, upload_logo=True)
    filled = slot_payload("uid-1", 1, 123)
    empty = slot_payload("uid-1", 2, None)
    p1 = await client.mirror_slot(filled, ev, dj, None)
    p2 = await client.mirror_slot(empty, ev, None, None)
    ev_map, dj_map = {ev: "uid-1"}, {dj: 123}
    assert notion_slot_payload(fake.pages[p1], ev_map, dj_map) == filled
    assert notion_slot_payload(fake.pages[p2], ev_map, dj_map) == empty
    # clearing a booked slot empties the relation
    await client.mirror_slot(slot_payload("uid-1", 1, None), ev, None, p1)
    assert notion_slot_payload(fake.pages[p1], ev_map, dj_map)["discord_id"] is None


async def test_mirror_event_recreates_trashed_page(env):
    fake, client, nids = env
    old = await client.mirror_event("uid-1", event_payload(EVENT_ROW), None)
    fake.trash_page(old)
    new = await client.mirror_event("uid-1", event_payload(EVENT_ROW), old)
    assert new != old
    snap = await client.read_all()
    assert [p["id"] for p in snap.events] == [new]


async def test_mirror_event_recreates_missing_page(env):
    fake, client, _ = env
    new = await client.mirror_event("uid-1", event_payload(EVENT_ROW), "gone-id")
    assert new in fake.pages


async def test_mirror_event_updates_existing_page_in_place(env):
    fake, client, _ = env
    page_id = await client.mirror_event("uid-1", event_payload(EVENT_ROW), None)
    again = await client.mirror_event(
        "uid-1", event_payload({**EVENT_ROW, "slot_count": 6}), page_id)
    assert again == page_id
    assert notion_event_payload(fake.pages[page_id])["slot_count"] == 6
    assert len(fake.pages) == 1


async def test_mirror_event_writes_bot_id(env):
    fake, client, _ = env
    page_id = await client.mirror_event("uid-1", event_payload(EVENT_ROW), None)
    assert fake.pages[page_id]["properties"]["Bot ID"]["rich_text"][0]["plain_text"] == "uid-1"


async def test_mirror_dj_keeps_logo_when_not_uploading(env):
    fake, client, _ = env
    page_id = await client.mirror_dj(
        dj_payload(DJ_ROW), None, logo_image=None, upload_logo=True, logo_url=DJ_ROW["logo_url"])
    changed = {**DJ_ROW, "dj_name": "DJ B", "logo_url": None}
    await client.mirror_dj(dj_payload(changed), page_id, logo_image=None, upload_logo=False)
    props = fake.pages[page_id]["properties"]
    assert props["DJ name"]["title"][0]["plain_text"] == "DJ B"
    assert props["Logo"]["files"][0]["external"]["url"] == "https://x.test/l.png"


async def test_mirror_dj_writes_discord_name_outside_the_payload(env):
    fake, client, _ = env
    page_id = await client.mirror_dj(
        dj_payload(DJ_ROW), None, logo_image=None, upload_logo=True, discord_name="alice")
    props = fake.pages[page_id]["properties"]
    assert props["Discord name"]["rich_text"][0]["plain_text"] == "alice"
    assert notion_dj_payload(fake.pages[page_id]) == dj_payload(DJ_ROW)
    # Unknown name (user not in the cache): the cell keeps its value
    await client.mirror_dj(dj_payload(DJ_ROW), page_id, logo_image=None, upload_logo=False)
    props = fake.pages[page_id]["properties"]
    assert props["Discord name"]["rich_text"][0]["plain_text"] == "alice"


async def test_trash_page_tolerates_404_and_read_all_skips_trashed(env):
    fake, client, _ = env
    page_id = await client.mirror_event("uid-1", event_payload(EVENT_ROW), None)
    await client.trash_page(page_id)
    await client.trash_page("does-not-exist")
    snap = await client.read_all()
    assert snap.events == [] and snap.botdata == []


async def test_has_rows_reads_one_page_and_skips_trashed(env):
    fake, client, _ = env
    assert not await client.has_rows("events")
    page_id = await client.mirror_event("uid-1", event_payload(EVENT_ROW), None)
    assert await client.has_rows("events")
    assert not await client.has_rows("djs") and not await client.has_rows("slots")
    fake.trash_page(page_id)
    assert not await client.has_rows("events")
