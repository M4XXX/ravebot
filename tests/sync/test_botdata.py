"""Bot data mirror: cycle push step and client chunking (SQL: tests/pg/test_botdata.py)."""
import json
from unittest.mock import AsyncMock, MagicMock

from bot.notion.api import NotionApi
from bot.notion.client import NotionClient
from bot.notion.schema import NotionIds
from bot.sync.botdata import parse_botdata_page
from tests.notion.fake_notion import FakeNotion
from tests.sync.pages import snapshot
from tests.sync.test_cycle import make_cycle, store  # noqa: F401

ROW = {"event_id": "E1", "discord_user_id": 5}


def bd_page(page_id: str, key: str, row: dict | None, table: str = "manual_events") -> dict:
    text = json.dumps(row, sort_keys=True) if row is not None else ""
    return {"id": page_id, "properties": {
        "Key": {"title": [{"plain_text": key}]},
        "Table": {"select": {"name": table}},
        "Payload": {"rich_text": [{"plain_text": text}] if text else []},
    }}


async def _cycle(store, pages):  # noqa: ANN001, ANN202, F811
    cycle, _ = make_cycle(snapshot())
    snap = snapshot()
    snap.botdata = pages
    cycle._ctx.notion_client.read_all = AsyncMock(return_value=snap)
    await cycle.run()


async def test_hand_edit_in_botdata_is_overwritten(store) -> None:  # noqa: F811
    store.botdata["manual_events:a"] = ("manual_events", ROW)
    store.sync("botdata", "manual_events:a", "p1", ROW)
    await _cycle(store, [bd_page("p1", "manual_events:a", ROW)])
    assert store.changed == []  # in sync
    await _cycle(store, [bd_page("p1", "manual_events:a", {**ROW, "event_id": "HAND"})])
    assert store.changed == [("botdata", "manual_events:a", "upsert")]
    assert not store.open()  # never a conflict


async def test_botdata_page_without_row_is_trashed(store) -> None:  # noqa: F811
    store.sync("botdata", "manual_events:gone", "p2", ROW)
    await _cycle(store, [bd_page("p2", "manual_events:gone", ROW), bd_page("p3", "x:y", ROW)])
    assert ("botdata", "manual_events:gone", "delete") in store.changed
    assert ("botdata", "stray:p3", "delete") in store.changed


async def test_payload_over_2000_chars_round_trips() -> None:
    fake = FakeNotion()
    api = NotionApi("k", MagicMock(post_error=AsyncMock(), log_attention=AsyncMock()),
                    transport=fake.transport)
    db = fake._create_database({
        "title": [{"text": {"content": "Bot data"}}], "parent": {},
        "initial_data_source": {"properties": {"Key": {"title": {}}}},
    }).json()
    ds = db["data_sources"][0]["id"]
    client = NotionClient(api, NotionIds("e", "s", "d", "e", "s", "d", "p", ds, db["id"]))
    row = {"id": 1, "data": {"text": "ü" * 4500}}
    page_id = await client.mirror_botdata("config:1", "config", row, None)
    chunks = fake.pages[page_id]["properties"]["Payload"]["rich_text"]
    assert len(chunks) == 3
    assert parse_botdata_page(fake.pages[page_id]) == ("config:1", "config", row)
