"""Tests for NotionClient.upsert_dj / refresh_discord_name and migration 0007."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.notion.api import NotionApi
from bot.notion.client import NotionClient, NotionWriteError
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
    return fake, NotionClient(api, nids), nids


def dj_pages(fake, nids):
    return [p for p in fake.pages.values()
            if p["parent"]["data_source_id"] == nids.djs_ds]


KW = dict(vrchat_name="vrc", logo_url="https://x.test/l.png",
          link="https://twitch.tv/a", alt_link=None)


async def test_upsert_creates_then_updates_without_duplicate(env):
    fake, client, nids = env
    first = await client.upsert_dj(123, "DJ A", "alice", **KW)
    assert len(dj_pages(fake, nids)) == 1
    props = fake.pages[first]["properties"]
    assert props["DJ name"]["title"][0]["text"]["content"] == "DJ A"
    assert props["Discord ID"]["rich_text"][0]["text"]["content"] == "123"
    assert props["Discord name"]["rich_text"][0]["text"]["content"] == "alice"
    assert props["VRChat name"]["rich_text"][0]["text"]["content"] == "vrc"
    assert props["Link"] == {"url": "https://twitch.tv/a"}
    assert props["Alternative link"] == {"url": None}
    assert fake.pages[first]["parent"] == {
        "type": "data_source_id", "data_source_id": nids.djs_ds}

    second = await client.upsert_dj(
        123, "DJ B", "alice2", vrchat_name=None, logo_url=None,
        link=None, alt_link="https://alt.test")
    assert second == first
    assert len(dj_pages(fake, nids)) == 1
    assert fake.requests_to("PATCH", f"/pages/{first}")
    props = fake.pages[first]["properties"]
    assert props["DJ name"]["title"][0]["plain_text"] == "DJ B"
    assert props["VRChat name"]["rich_text"] == []
    assert props["Logo"] == {"files": []}
    assert props["Alternative link"] == {"url": "https://alt.test"}

    other = await client.upsert_dj(456, "DJ C", "carol", **KW)
    assert other != first
    assert len(dj_pages(fake, nids)) == 2


async def test_upsert_matches_client_side_when_filter_ignored(env):
    fake, client, nids = env
    await client.upsert_dj(123, "DJ A", "a", **KW)
    fake.ignore_query_filters = True
    await client.upsert_dj(1234, "DJ B", "b", **KW)
    assert len(dj_pages(fake, nids)) == 2


async def test_upsert_ignores_trashed_row(env):
    fake, client, nids = env
    first = await client.upsert_dj(123, "DJ A", "a", **KW)
    fake.pages[first]["in_trash"] = True
    second = await client.upsert_dj(123, "DJ A", "a", **KW)
    assert second != first


@pytest.mark.parametrize("url,written", [
    ("https://x.test/l.png", True), ("http://x.test/l.png", True),
    ("ftp://x.test/l.png", False), ("not a url", False), ("", False), (None, False),
])
async def test_logo_and_links_only_for_http_urls(env, url, written):
    fake, client, nids = env
    pid = await client.upsert_dj(
        1, "D", "d", vrchat_name=None, logo_url=url, link=url, alt_link=url)
    props = fake.pages[pid]["properties"]
    if written:
        assert props["Logo"] == {"files": [
            {"name": "logo", "type": "external", "external": {"url": url}}]}
        assert props["Link"] == {"url": url}
        assert props["Alternative link"] == {"url": url}
    else:
        assert props["Logo"] == {"files": []}
        assert props["Link"] == {"url": None}
        assert props["Alternative link"] == {"url": None}


async def test_refresh_discord_name_patches_only_that_property(env):
    fake, client, nids = env
    pid = await client.upsert_dj(1, "D", "old", **KW)
    n = len(fake.requests)
    await client.refresh_discord_name(pid, "new")
    reqs = fake.requests[n:]
    assert len(reqs) == 1
    method, path, body = reqs[0]
    assert (method, path) == ("PATCH", f"/pages/{pid}")
    assert list(body["properties"]) == ["Discord name"]
    assert fake.pages[pid]["properties"]["Discord name"]["rich_text"][0]["plain_text"] == "new"


def test_migration_0007_clears_notion_dj_page_ids():
    path = Path(__file__).parents[2] / "migrations/versions/0007_clear_notion_dj_page_ids.py"
    spec = importlib.util.spec_from_file_location("mig0007", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0007", "0006")
    with patch.object(mod, "op") as op:
        mod.upgrade()
    op.execute.assert_called_once_with("UPDATE djs SET notion_dj_page_id = NULL")
    with patch.object(mod, "op") as op:
        mod.downgrade()
    assert not op.method_calls


async def test_dj_page_alive_true_for_live_page(env):
    fake, client, nids = env
    pid = await client.upsert_dj(1, "D", "d", **KW)
    assert await client.dj_page_alive(pid) is True
    assert fake.requests[-1][:2] == ("GET", f"/pages/{pid}")


async def test_dj_page_alive_false_for_trashed_page(env):
    fake, client, nids = env
    pid = await client.upsert_dj(1, "D", "d", **KW)
    fake.trash_page(pid)
    assert await client.dj_page_alive(pid) is False


async def test_dj_page_alive_false_for_archived_page(env):
    fake, client, nids = env
    pid = await client.upsert_dj(1, "D", "d", **KW)
    fake.pages[pid]["is_archived"] = True
    assert await client.dj_page_alive(pid) is False


async def test_dj_page_alive_false_for_missing_page_without_admin_error(env):
    fake, client, nids = env
    assert await client.dj_page_alive("no-such-page") is False
    client._api.log.post_error.assert_not_awaited()


async def test_dj_page_alive_other_error_raises(env):
    fake, client, nids = env
    pid = await client.upsert_dj(1, "D", "d", **KW)
    fake.fail_once("GET", f"/pages/{pid}", 403)
    with pytest.raises(NotionWriteError):
        await client.dj_page_alive(pid)
