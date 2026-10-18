"""DJs relation without lost updates, and Notion retries that never duplicate a page.

A small in-memory Notion (httpx.MockTransport) holds one event page; GETs are delayed so
concurrent bookings would interleave without the per-event lock.
"""
import asyncio
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.notion.client import NotionClient, NotionWriteError, _refuse_deletion

# The fake server keeps the real sleep; tests patch asyncio.sleep to skip backoff waits.
_real_sleep = asyncio.sleep


class FakeNotion:
    def __init__(self, relation: list[str], page_size: int = 100) -> None:
        self.relation = list(relation)
        self.page_size = page_size
        self.requests: list[tuple[str, str]] = []
        self.pages: list[dict] = []
        self.fail_post_with: Exception | None = None
        self.status_for_patch: int | None = None
        self.retry_after: str | None = None
        self.failing_patches = 0                   # the next n PATCHes answer with 429
        self.human_link_during_wait: str | None = None
        self.applied_patches_answer: int | None = None  # PATCH applied, answered with this
        self.post_status: int | None = None        # POST applied, answered with this

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path.endswith("/properties/djs"):
            await _real_sleep(0.01)
            start = int(request.url.params.get("start_cursor", "0"))
            chunk = self.relation[start:start + self.page_size]
            more = start + self.page_size < len(self.relation)
            return httpx.Response(200, json={
                "results": [{"relation": {"id": pid}} for pid in chunk],
                "has_more": more, "next_cursor": str(start + self.page_size) if more else None,
            })
        if request.method == "GET" and path.startswith("/v1/pages/"):
            await _real_sleep(0.01)  # widen the read-modify-write window
            more = len(self.relation) > self.page_size
            return httpx.Response(200, json={"properties": {"DJs": {
                "id": "djs", "has_more": more,
                "relation": [{"id": pid} for pid in self.relation[: self.page_size]],
            }}})
        if request.method == "PATCH":
            if self.failing_patches:
                self.failing_patches -= 1
                if self.human_link_during_wait:
                    self.relation.append(self.human_link_during_wait)  # organizer edits by hand
                return httpx.Response(429, headers={"Retry-After": "1"}, json={})
            if self.status_for_patch:
                headers = {"Retry-After": self.retry_after} if self.retry_after else {}
                return httpx.Response(self.status_for_patch, headers=headers, json={})
            body = json.loads(request.content)
            self.relation = [r["id"] for r in body["properties"]["DJs"]["relation"]]
            if self.applied_patches_answer:
                return httpx.Response(self.applied_patches_answer, json={})
            return httpx.Response(200, json={"id": "evt"})
        if request.method == "POST" and path.endswith("/query"):
            return httpx.Response(200, json={"results": self.pages[:1]})
        if request.method == "POST" and path == "/v1/pages":
            self.pages.append({"id": f"page-{len(self.pages) + 1}"})
            if self.fail_post_with is not None:
                raise self.fail_post_with  # created on Notion's side, response lost
            if self.post_status is not None:
                return httpx.Response(self.post_status, json={})  # created, but a 502/504
            return httpx.Response(200, json=self.pages[-1])
        return httpx.Response(404, json={})


def make_client(fake: FakeNotion) -> NotionClient:
    log = MagicMock()
    log.post_error = AsyncMock()
    client = NotionClient(api_key="secret_test", database_id="db", log_service=log)
    client._client = httpx.AsyncClient(
        base_url="https://api.notion.com/v1",
        transport=httpx.MockTransport(fake.handler),
        event_hooks={"request": [_refuse_deletion]},
    )
    return client


@pytest.mark.asyncio
async def test_concurrent_bookings_keep_every_dj() -> None:
    fake = FakeNotion(relation=["page-admin"])
    client = make_client(fake)
    await asyncio.gather(
        client.write_booking("evt", 0, "page-A", [], "Rave", 0),
        client.write_booking("evt", 1, "page-B", [], "Rave", 0),
    )
    assert sorted(fake.relation) == ["page-A", "page-B", "page-admin"]


@pytest.mark.asyncio
async def test_cancel_removes_only_this_dj() -> None:
    fake = FakeNotion(relation=["page-A", "page-admin", "page-B"])
    client = make_client(fake)
    await client.delete_booking("evt", "page-A", ["page-A"], "Rave", 0)
    assert fake.relation == ["page-admin", "page-B"]


@pytest.mark.asyncio
async def test_dashless_ids_match_dashed_ids() -> None:
    dashed = "3fc898b5-0017-815a-b41a-de39dbf4013f"
    fake = FakeNotion(relation=[dashed])
    client = make_client(fake)
    await client.write_booking("evt", 0, dashed.replace("-", ""), [], "Rave", 0)
    assert fake.relation == [dashed]  # recognised as already linked, no duplicate
    await client.delete_booking("evt", dashed.replace("-", ""), [], "Rave", 0)
    assert fake.relation == []


@pytest.mark.asyncio
async def test_long_relations_are_read_completely() -> None:
    fake = FakeNotion(relation=[f"page-{i}" for i in range(30)], page_size=25)
    client = make_client(fake)
    await client.write_booking("evt", 0, "page-new", [], "Rave", 0)
    assert len(fake.relation) == 31 and fake.relation[-1] == "page-new"


@pytest.mark.asyncio
async def test_timeout_after_create_does_not_create_twice() -> None:
    fake = FakeNotion(relation=[])
    fake.fail_post_with = httpx.ReadTimeout("response lost")
    client = make_client(fake)
    page_id = await client.create_event("Rave Night 24 Oct 2026", datetime.date(2026, 10, 24))
    assert page_id == "page-1"
    assert [r for r in fake.requests if r == ("POST", "/v1/pages")] == [("POST", "/v1/pages")]


@pytest.mark.asyncio
async def test_client_error_is_not_retried() -> None:
    fake = FakeNotion(relation=[])
    fake.status_for_patch = 400
    client = make_client(fake)
    with patch("bot.notion.client.asyncio.sleep", new=AsyncMock()), pytest.raises(NotionWriteError):
        await client.write_booking("evt", 0, "page-A", [], "Rave", 0)
    assert [r[0] for r in fake.requests].count("PATCH") == 1
    client._log.post_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_waits_for_retry_after() -> None:
    fake = FakeNotion(relation=[])
    fake.status_for_patch = 429
    fake.retry_after = "7"
    client = make_client(fake)
    sleep = AsyncMock()
    with patch("bot.notion.client.asyncio.sleep", new=sleep), pytest.raises(NotionWriteError):
        await client.write_booking("evt", 0, "page-A", [], "Rave", 0)
    assert [r[0] for r in fake.requests].count("PATCH") == 3
    assert all(call.args[0] >= 7 for call in sleep.await_args_list)


@pytest.mark.asyncio
async def test_retry_rereads_the_relation_so_a_hand_edit_during_the_wait_survives() -> None:
    fake = FakeNotion(relation=["page-admin"])
    fake.failing_patches = 1
    fake.human_link_during_wait = "page-HUMAN"
    client = make_client(fake)
    with patch("bot.notion.client.asyncio.sleep", new=AsyncMock()):
        await client.write_booking("evt", 0, "page-A", [], "Rave", 0)
    assert fake.relation == ["page-admin", "page-HUMAN", "page-A"]


@pytest.mark.asyncio
async def test_long_retry_after_is_capped() -> None:
    fake = FakeNotion(relation=[])
    fake.status_for_patch = 429
    fake.retry_after = "600"
    client = make_client(fake)
    sleep = AsyncMock()
    with patch("bot.notion.client.asyncio.sleep", new=sleep), pytest.raises(NotionWriteError):
        await client.write_booking("evt", 0, "page-A", [], "Rave", 0)
    assert sleep.await_args_list and all(c.args[0] <= 10 for c in sleep.await_args_list)


@pytest.mark.asyncio
async def test_applied_patch_answered_with_5xx_is_not_sent_again() -> None:
    fake = FakeNotion(relation=[])
    fake.applied_patches_answer = 502
    client = make_client(fake)
    with patch("bot.notion.client.asyncio.sleep", new=AsyncMock()):
        await client.write_booking("evt", 0, "page-A", [], "Rave", 0)
    assert fake.relation == ["page-A"]
    assert [r[0] for r in fake.requests].count("PATCH") == 1
    client._log.post_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_5xx_after_create_does_not_create_twice() -> None:
    fake = FakeNotion(relation=[])
    fake.post_status = 504
    client = make_client(fake)
    with patch("bot.notion.client.asyncio.sleep", new=AsyncMock()):
        page_id = await client.create_event("Rave Night 24 Oct 2026", datetime.date(2026, 10, 24))
    assert page_id == "page-1"
    assert len(fake.pages) == 1


# Story 8.2: per-event locks are dropped once no writer holds or awaits them


def assert_no_event_locks(client: NotionClient) -> None:
    assert client._event_locks == {}
    assert client._event_lock_users == {}


@pytest.mark.asyncio
async def test_event_locks_are_removed_after_writes() -> None:
    fake = FakeNotion(relation=["page-admin"])
    client = make_client(fake)
    await asyncio.gather(
        client.write_booking("evt", 0, "page-A", [], "Rave", 0),
        client.write_booking("evt", 1, "page-B", [], "Rave", 0),
    )
    assert sorted(fake.relation) == ["page-A", "page-B", "page-admin"]
    assert_no_event_locks(client)
    await client.delete_booking("evt", "page-A", [], "Rave", 0)
    assert_no_event_locks(client)


@pytest.mark.asyncio
async def test_event_lock_serialises_writers_and_then_disappears() -> None:
    client = make_client(FakeNotion(relation=[]))
    order: list[str] = []
    first_inside = asyncio.Event()

    async def writer(name: str) -> None:
        async with client._event_lock("evt"):
            order.append(f"{name}-in")
            first_inside.set()
            await _real_sleep(0.01)
            order.append(f"{name}-out")

    task_a = asyncio.create_task(writer("a"))
    await first_inside.wait()
    task_b = asyncio.create_task(writer("b"))
    await _real_sleep(0)
    assert client._event_lock_users == {"evt": 2}  # b waits and keeps the entry alive
    await asyncio.gather(task_a, task_b)
    assert order == ["a-in", "a-out", "b-in", "b-out"]
    assert_no_event_locks(client)


@pytest.mark.asyncio
async def test_cancelled_waiter_leaves_no_event_lock() -> None:
    client = make_client(FakeNotion(relation=[]))
    release = asyncio.Event()
    holding = asyncio.Event()

    async def holder() -> None:
        async with client._event_lock("evt"):
            holding.set()
            await release.wait()

    async def waiter() -> None:
        async with client._event_lock("evt"):
            pass  # never reached

    hold_task = asyncio.create_task(holder())
    await holding.wait()
    wait_task = asyncio.create_task(waiter())
    await _real_sleep(0)
    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task
    assert client._event_lock_users == {"evt": 1}
    release.set()
    await hold_task
    assert_no_event_locks(client)


@pytest.mark.asyncio
async def test_failed_write_leaves_no_event_lock() -> None:
    fake = FakeNotion(relation=[])
    fake.status_for_patch = 400
    client = make_client(fake)
    with pytest.raises(NotionWriteError):
        await client.write_booking("evt", 0, "page-A", [], "Rave", 0)
    assert_no_event_locks(client)
