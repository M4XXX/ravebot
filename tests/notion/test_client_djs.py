"""Tests for NotionClient.upsert_dj / refresh_discord_name and migration 0007."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.notion.api import NotionApi
from bot.notion.client import DjLogoUploadError, NotionClient, NotionWriteError
from bot.notion.schema import NotionIds
from tests.notion.fake_notion import FakeNotion


@pytest.fixture
def env():
    fake = FakeNotion()
    log = MagicMock()
    log.post_error = AsyncMock()
    log.log_attention = AsyncMock()
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


async def test_upsert_with_logo_image_writes_file_upload(env):
    fake, client, nids = env
    client._api.upload_file = AsyncMock(return_value="up-9")
    await client.upsert_dj(123, "DJ A", "alice", **{**KW, "logo_url": None}, logo_image=b"img")
    client._api.upload_file.assert_awaited_once()
    assert client._api.upload_file.await_args.args[:3] == ("logo.png", "image/png", b"img")
    body = fake.requests_to("POST", "/pages")[-1][2]
    files = body["properties"]["Logo"]["files"]
    assert files == [{"name": "logo.png", "type": "file_upload", "file_upload": {"id": "up-9"}}]


NO_LOGO_KW = {**KW, "logo_url": None}


async def test_upsert_create_with_failing_upload_creates_row_without_logo(env):
    fake, client, nids = env
    client._api.upload_file = AsyncMock(side_effect=NotionWriteError("too big", 413))
    with pytest.raises(DjLogoUploadError) as e:
        await client.upsert_dj(123, "DJ A", "alice", **NO_LOGO_KW, logo_image=b"img")
    pages = dj_pages(fake, nids)
    assert len(pages) == 1
    assert e.value.page_id == pages[0]["id"]
    body = fake.requests_to("POST", "/pages")[-1][2]
    assert "Logo" not in body["properties"]
    assert body["properties"]["DJ name"]["title"][0]["text"]["content"] == "DJ A"


async def test_upsert_update_with_failing_upload_leaves_logo_untouched(env):
    fake, client, nids = env
    first = await client.upsert_dj(123, "DJ A", "alice", **KW)
    client._api.upload_file = AsyncMock(side_effect=NotionWriteError("too big", 413))
    with pytest.raises(DjLogoUploadError) as e:
        await client.upsert_dj(123, "DJ B", "alice", **NO_LOGO_KW, logo_image=b"img")
    assert e.value.page_id == first
    body = fake.requests_to("PATCH", f"/pages/{first}")[-1][2]
    assert "Logo" not in body["properties"]
    assert fake.pages[first]["properties"]["DJ name"]["title"][0]["plain_text"] == "DJ B"
    assert len(dj_pages(fake, nids)) == 1


async def test_upsert_write_rejecting_the_file_is_retried_without_logo(env):
    fake, client, nids = env
    client._api.upload_file = AsyncMock(return_value="up-1")
    fake.fail_once("POST", "/pages", 400)
    with pytest.raises(DjLogoUploadError) as e:
        await client.upsert_dj(123, "DJ A", "alice", **NO_LOGO_KW, logo_image=b"img")
    bodies = [r[2] for r in fake.requests_to("POST", "/pages")]
    assert "Logo" in bodies[-2]["properties"]
    assert "Logo" not in bodies[-1]["properties"]
    assert e.value.page_id == dj_pages(fake, nids)[0]["id"]


async def test_upsert_write_failure_without_logo_still_raises_write_error(env):
    fake, client, nids = env
    fake.fail_once("POST", "/pages", 400)
    with pytest.raises(NotionWriteError):
        await client.upsert_dj(123, "DJ A", "alice", **NO_LOGO_KW)
    assert len(fake.requests_to("POST", "/pages")) == 1


async def test_upsert_without_upload_logo_leaves_logo_out_on_update(env):
    fake, client, nids = env
    first = await client.upsert_dj(123, "DJ A", "alice", **KW)
    client._api.upload_file = AsyncMock(return_value="up-2")
    again = await client.upsert_dj(
        123, "DJ B", "alice", **NO_LOGO_KW, logo_image=b"img", upload_logo=False
    )
    assert again == first
    client._api.upload_file.assert_not_awaited()
    body = fake.requests_to("PATCH", f"/pages/{first}")[-1][2]
    assert "Logo" not in body["properties"]


async def test_upsert_without_upload_logo_still_sends_image_for_new_row(env):
    fake, client, nids = env
    client._api.upload_file = AsyncMock(return_value="up-3")
    await client.upsert_dj(
        123, "DJ A", "alice", **NO_LOGO_KW, logo_image=b"img", upload_logo=False
    )
    client._api.upload_file.assert_awaited_once()
    body = fake.requests_to("POST", "/pages")[-1][2]
    assert body["properties"]["Logo"]["files"][0]["file_upload"] == {"id": "up-3"}


async def test_booking_page_lookup_survives_failing_logo_upload(env):
    """ensure_dj_page (used by BookingConfirmView) on the real client: the logo
    upload fails, yet a page id comes back and is stored, so the booking goes on."""
    from bot.services.dj_pages import ensure_dj_page

    fake, client, nids = env
    client._api.upload_file = AsyncMock(side_effect=NotionWriteError("too big", 413))
    row = {"dj_name": "DJ A", "vrchat_name": None, "logo_url": None,
           "logo_image": b"img", "stream_url_primary": "https://twitch.tv/a",
           "stream_url_backup": None}
    with patch("bot.services.dj_pages.get_notion_dj_page_id", new=AsyncMock(return_value=None)), \
         patch("bot.services.dj_pages.get_dj_by_discord_id", new=AsyncMock(return_value=row)), \
         patch("bot.services.dj_pages.set_notion_dj_page_id", new=AsyncMock()) as setp, \
         patch("bot.services.dj_pages.set_logo_synced", new=AsyncMock()) as setl:
        page_id = await ensure_dj_page(MagicMock(), client, 123, "alice")
    assert page_id is not None
    assert [p["id"] for p in dj_pages(fake, nids)] == [page_id]
    setp.assert_awaited_once()
    setl.assert_awaited_once()
    assert setl.await_args.args[1:] == (123, False)
