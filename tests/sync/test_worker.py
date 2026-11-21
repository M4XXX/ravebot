"""Tests for bot/sync/worker.py: drain order, stalls, deletes, logos and changed().

The outbox and mirror repositories are replaced by an in-memory store; the real
SQL is covered in tests/pg/test_outbox.py.
"""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db.outbox import OutboxRow
from bot.notion.client import DjLogoUploadError
from bot.sync import worker as worker_mod
from bot.sync.worker import MirrorWorker, changed

T0 = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.UTC)
DJ = 111111111111111111

EVENT_ROW = {
    "uid": "u1", "event_id": "RAVE-1", "date": datetime.date(2026, 12, 1),
    "start": "20:00", "slot_minutes": 60, "slot_count": 2,
    "status": None, "genre": None, "notes": None,
}
DJ_ROW = {
    "discord_user_id": DJ, "dj_name": "DJ A", "vrchat_name": None,
    "stream_url_primary": None, "stream_url_backup": None,
    "logo_url": None, "logo_image": None, "logo_synced": True,
}


class Store:
    """In-memory outbox + mirror + Postgres rows used by the worker."""

    def __init__(self) -> None:
        self.queue: dict[tuple[str, str], OutboxRow] = {}
        self.pages: dict[tuple[str, str], str] = {}
        self.synced: dict[tuple[str, str], dict] = {}
        self.conflicts: set[tuple[str, str]] = set()
        self.failures: dict[tuple[str, str], str] = {}
        self.events: dict[str, dict] = {}
        self.holders: dict[tuple[str, int], int | None] = {}
        self.djs: dict[int, dict] = {}
        self.logo_marked: list[tuple[int, bytes | None]] = []
        self._tick = 0

    def add(self, entity: str, key: str, op: str = "upsert", page_id: str | None = None) -> None:
        self._tick += 1
        self.queue[(entity, key)] = OutboxRow(
            entity, key, op, page_id, T0 + datetime.timedelta(seconds=self._tick), 0, T0, None
        )

    # outbox
    async def due(self, db, limit):  # noqa: ANN001, ANN202
        return list(self.queue.values())[:limit]

    async def done(self, db, entity, key, enqueued_at):  # noqa: ANN001, ANN202
        if self.queue.get((entity, key)) and self.queue[(entity, key)].enqueued_at == enqueued_at:
            del self.queue[(entity, key)]

    async def failed(self, db, entity, key, error, enqueued_at=None):  # noqa: ANN001, ANN202
        self.failures[(entity, key)] = error

    async def enqueue(self, db, entity, key, op="upsert", notion_page_id=None):  # noqa: ANN001, ANN202
        self.add(entity, key, op, notion_page_id)

    # mirror
    async def get(self, db, entity, key):  # noqa: ANN001, ANN202
        page = self.pages.get((entity, key))
        return SimpleNamespace(notion_page_id=page) if page else None

    async def set_synced(self, db, entity, key, page_id, payload):  # noqa: ANN001, ANN202
        self.pages[(entity, key)] = page_id
        self.synced[(entity, key)] = payload

    async def forget(self, db, entity, key):  # noqa: ANN001, ANN202
        self.pages.pop((entity, key), None)
        self.synced.pop((entity, key), None)

    async def open_issue_keys(self, db):  # noqa: ANN001, ANN202
        return set(self.conflicts)

    # djs
    async def get_dj_by_discord_id(self, db, discord_id):  # noqa: ANN001, ANN202
        return self.djs.get(discord_id)

    async def mark_logo_synced_if_unchanged(self, db, user_id, image):  # noqa: ANN001, ANN202
        self.logo_marked.append((user_id, image))


class FakeDb:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def fetchrow(self, sql, uid):  # noqa: ANN001, ANN202
        assert "FROM events" in sql
        return self.store.events.get(uid)

    async def fetchval(self, sql, event_id, slot_index):  # noqa: ANN001, ANN202
        assert "FROM slot_locks" in sql
        return self.store.holders.get((event_id, slot_index))


class FakeNotion:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.discord_names: list[str | None] = []

    async def mirror_event(self, uid, payload, page_id):  # noqa: ANN001, ANN202
        self.calls.append(("event", uid, page_id))
        return page_id or f"page-ev-{uid}"

    async def mirror_dj(  # noqa: ANN202
        self, payload, page_id, *, logo_image, upload_logo, logo_url=None, discord_name=None  # noqa: ANN001
    ):
        self.calls.append(("dj", payload["discord_id"], page_id, upload_logo))
        self.discord_names.append(discord_name)
        return page_id or f"page-dj-{payload['discord_id']}"

    async def mirror_slot(self, payload, event_page_id, dj_page_id, page_id, *, event_label=None):  # noqa: ANN001, ANN202
        key = f"{payload['event_uid']}:{payload['number'] - 1}"
        self.calls.append(("slot", key, event_page_id, dj_page_id, event_label))
        return page_id or f"page-sl-{key}"

    async def trash_page(self, page_id):  # noqa: ANN001, ANN202
        self.calls.append(("trash", page_id))


@pytest.fixture
def store():
    s = Store()
    fake_outbox = SimpleNamespace(
        due=s.due, done=s.done, failed=s.failed, enqueue=s.enqueue, OutboxRow=OutboxRow
    )
    fake_mirror = SimpleNamespace(
        get=s.get, set_synced=s.set_synced, forget=s.forget, open_issue_keys=s.open_issue_keys
    )
    fake_djs = SimpleNamespace(
        get_dj_by_discord_id=s.get_dj_by_discord_id,
        mark_logo_synced_if_unchanged=s.mark_logo_synced_if_unchanged,
    )
    with patch.object(worker_mod, "outbox", fake_outbox), \
         patch.object(worker_mod, "mirror", fake_mirror), \
         patch.object(worker_mod, "djs", fake_djs):
        yield s


def _worker(store: Store, notion: FakeNotion | None = None) -> MirrorWorker:
    return MirrorWorker(FakeDb(store), notion or FakeNotion(), None, rate=1e9)


async def test_drain_writes_event_then_slots(store: Store) -> None:
    store.events["u1"] = EVENT_ROW
    store.djs[DJ] = DJ_ROW
    store.holders[("u1", 1)] = DJ  # slot_locks is keyed by the event uid
    # Queued in the "wrong" order: slots first.
    store.add("slot", "u1:0")
    store.add("slot", "u1:1")
    store.add("dj", str(DJ))
    store.add("event", "u1")
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 4

    assert [c[0] for c in notion.calls] == ["event", "dj", "slot", "slot"]
    assert notion.calls[2] == ("slot", "u1:0", "page-ev-u1", None, "RAVE-1")
    assert notion.calls[3] == ("slot", "u1:1", "page-ev-u1", f"page-dj-{DJ}", "RAVE-1")
    assert store.queue == {}
    assert store.synced[("slot", "u1:1")] == {"event_uid": "u1", "number": 2, "discord_id": DJ}
    assert store.synced[("event", "u1")]["event_id"] == "RAVE-1"
    assert store.pages[("dj", str(DJ))] == f"page-dj-{DJ}"


async def test_drain_skips_open_conflict_key(store: Store) -> None:
    store.events["u1"] = EVENT_ROW
    store.events["u2"] = {**EVENT_ROW, "uid": "u2", "event_id": "RAVE-2"}
    store.add("event", "u1")
    store.add("event", "u2")
    store.conflicts.add(("event", "u1"))
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert notion.calls == [("event", "u2", None)]
    assert ("event", "u1") in store.queue  # stays queued until the conflict is decided
    assert ("event", "u1") not in store.failures


async def test_slot_writes_missing_event_page_first(store: Store) -> None:
    store.events["u1"] = EVENT_ROW
    store.add("slot", "u1:0")
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert [c[0] for c in notion.calls] == ["event", "slot"]
    assert store.pages[("event", "u1")] == "page-ev-u1"


async def test_slot_fails_when_event_write_fails(store: Store) -> None:
    store.events["u1"] = EVENT_ROW
    store.add("slot", "u1:0")
    notion = FakeNotion()
    notion.mirror_event = AsyncMock(side_effect=RuntimeError("notion down"))

    assert await _worker(store, notion).drain_once() == 0

    assert store.failures == {("slot", "u1:0"): "notion down"}
    assert ("slot", "u1:0") in store.queue
    assert all(c[0] != "slot" for c in notion.calls)


async def test_failure_of_one_row_does_not_stop_the_rest(store: Store) -> None:
    store.events["u2"] = {**EVENT_ROW, "uid": "u2", "event_id": "RAVE-2"}
    store.add("event", "gone-but-bad")
    store.add("event", "u2")
    notion = FakeNotion()
    notion.trash_page = AsyncMock(side_effect=RuntimeError("trash failed"))
    store.pages[("event", "gone-but-bad")] = "p-old"

    assert await _worker(store, notion).drain_once() == 1

    assert store.failures == {("event", "gone-but-bad"): "trash failed"}
    assert ("event", "u2") not in store.queue


async def test_upsert_of_deleted_event_trashes_mirror_page(store: Store) -> None:
    store.pages[("event", "u9")] = "p-u9"
    store.add("event", "u9")
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert notion.calls == [("trash", "p-u9")]
    assert ("event", "u9") not in store.pages


async def test_delete_uses_outbox_page_id(store: Store) -> None:
    store.add("dj", str(DJ), op="delete", page_id="p-dj")
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert notion.calls == [("trash", "p-dj")]


async def test_slot_of_deleted_event_without_mirror_is_dropped(store: Store) -> None:
    store.add("slot", "gone:0")
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert notion.calls == []
    assert store.queue == {}


async def test_slot_beyond_slot_count_is_trashed(store: Store) -> None:
    store.events["u1"] = EVENT_ROW  # slot_count 2
    store.pages[("slot", "u1:2")] = "p-s2"
    store.add("slot", "u1:2")
    notion = FakeNotion()

    await _worker(store, notion).drain_once()

    assert notion.calls == [("trash", "p-s2")]


async def test_dj_logo_uploaded_when_unsynced_and_marked(store: Store) -> None:
    store.djs[DJ] = {**DJ_ROW, "logo_image": b"png", "logo_synced": False}
    store.add("dj", str(DJ))
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert notion.calls == [("dj", DJ, None, True)]
    assert store.logo_marked == [(DJ, b"png")]


async def test_dj_discord_name_comes_from_the_resolver(store: Store) -> None:
    store.djs[DJ] = DJ_ROW
    store.djs[DJ + 1] = {**DJ_ROW, "discord_user_id": DJ + 1, "dj_name": "DJ B"}
    store.add("dj", str(DJ))
    store.add("dj", str(DJ + 1))
    notion = FakeNotion()
    names = {DJ: "alice"}
    worker = MirrorWorker(FakeDb(store), notion, None, rate=1e9, name_resolver=names.get)

    assert await worker.drain_once() == 2

    assert notion.discord_names == ["alice", None]
    # The Discord name is not part of the compared payload
    assert "discord_name" not in store.synced[("dj", str(DJ))]


async def test_dj_discord_name_resolver_failure_writes_no_name(store: Store) -> None:
    store.djs[DJ] = DJ_ROW
    store.add("dj", str(DJ))
    notion = FakeNotion()

    def boom(_uid: int) -> str | None:
        raise RuntimeError("no cache")

    worker = MirrorWorker(FakeDb(store), notion, None, rate=1e9, name_resolver=boom)

    assert await worker.drain_once() == 1
    assert notion.discord_names == [None]


async def test_dj_without_resolver_writes_no_name(store: Store) -> None:
    store.djs[DJ] = DJ_ROW
    store.add("dj", str(DJ))
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1
    assert notion.discord_names == [None]


async def test_dj_logo_failure_keeps_row_and_retries(store: Store) -> None:
    store.djs[DJ] = {**DJ_ROW, "logo_image": b"png", "logo_synced": False}
    store.add("dj", str(DJ))
    notion = FakeNotion()
    notion.mirror_dj = AsyncMock(side_effect=DjLogoUploadError("p-new"))

    assert await _worker(store, notion).drain_once() == 0

    assert store.pages[("dj", str(DJ))] == "p-new"
    assert ("dj", str(DJ)) in store.failures
    assert ("dj", str(DJ)) in store.queue
    assert store.logo_marked == []


async def test_rate_limiter_spaces_requests() -> None:
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = worker_mod._RateLimiter(3.0, clock=lambda: now[0], sleep=sleep)
    await limiter.acquire(2)
    await limiter.acquire(2)
    await limiter.acquire(1)
    assert slept == pytest.approx([2 / 3, 2 / 3])


async def test_loop_drains_after_notify_and_survives_errors(store: Store) -> None:
    w = _worker(store)
    calls = []

    async def drain() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db gone")
        return 0

    w.drain_once = drain  # type: ignore[method-assign]
    w.start()
    w.notify()
    for _ in range(50):
        await asyncio.sleep(0)
    w.notify()
    for _ in range(50):
        await asyncio.sleep(0)
    await w.stop()
    assert len(calls) == 2


async def test_changed_returns_without_awaiting_db() -> None:
    never = asyncio.Event()

    async def blocked_enqueue(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        await never.wait()

    w = MagicMock()
    ctx = SimpleNamespace(mirror=w)
    with patch.object(worker_mod.outbox, "enqueue", new=blocked_enqueue):
        changed(ctx, "event", "u1")  # returns at once although enqueue never finishes
        assert len(worker_mod._pending) == 1
        await asyncio.sleep(0)
        w.notify.assert_not_called()
        tasks = list(worker_mod._pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert worker_mod._pending == set()


async def test_changed_enqueues_and_notifies() -> None:
    w = MagicMock()
    w.db = object()
    enqueue = AsyncMock()
    with patch.object(worker_mod.outbox, "enqueue", new=enqueue):
        changed(SimpleNamespace(mirror=w), "dj", "42", "delete", "p1")
        await asyncio.gather(*worker_mod._pending)
    enqueue.assert_awaited_once_with(w.db, "dj", "42", "delete", "p1")
    w.notify.assert_called_once()


async def test_changed_is_noop_without_notion() -> None:
    enqueue = AsyncMock()
    with patch.object(worker_mod.outbox, "enqueue", new=enqueue):
        changed(SimpleNamespace(mirror=None), "event", "u1")
        await asyncio.sleep(0)
    enqueue.assert_not_called()
    assert worker_mod._pending == set()


async def test_slot_of_deleted_dj_is_mirrored_as_notion_holds_it(store: Store) -> None:
    """I3: a past booking of a deleted DJ (no djs row) is written and recorded as free.

    Notion gets no DJ relation, so the recorded payload must say so too; otherwise
    the next poll reads Notion as a hand edit that frees the booking.
    """
    store.events["u1"] = EVENT_ROW
    store.holders[("u1", 0)] = DJ  # DJ deleted, the past booking stays
    store.pages[("event", "u1")] = "page-ev-u1"
    store.add("slot", "u1:0")
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert notion.calls == [("slot", "u1:0", "page-ev-u1", None, "RAVE-1")]
    assert store.synced[("slot", "u1:0")] == {"event_uid": "u1", "number": 1, "discord_id": None}


@pytest.mark.parametrize("entity", ["event", "dj", "slot"])
async def test_stray_page_is_trashed(store: Store, entity: str) -> None:
    """I4: a page with no bot record (queued by the post-restore cycle) is trashed."""
    store.add(entity, "stray:p9", "delete", "p9")
    notion = FakeNotion()

    assert await _worker(store, notion).drain_once() == 1

    assert notion.calls == [("trash", "p9")]
    assert store.failures == {}


async def test_conflict_opened_while_waiting_for_the_gate_stalls_the_write(
    store: Store,
) -> None:
    """C1: a sync cycle that held the gate may have opened a conflict meanwhile."""
    store.events["u1"] = EVENT_ROW
    store.add("event", "u1")
    notion = FakeNotion()
    worker = _worker(store, notion)

    async with worker.gate:  # a sync cycle runs
        task = asyncio.create_task(worker.drain_once())
        await asyncio.sleep(0.01)
        assert notion.calls == []  # the worker waits
        store.conflicts.add(("event", "u1"))  # the cycle opened a conflict
    assert await task == 0
    assert notion.calls == []
    assert ("event", "u1") in store.queue


async def test_stop_waits_for_queued_changes(store: Store) -> None:
    """A change queued just before shutdown reaches the outbox before the pool closes."""
    import asyncio

    from bot.sync import worker as worker_mod

    done = asyncio.Event()

    async def slow_enqueue() -> None:
        await asyncio.sleep(0.05)
        done.set()

    task = asyncio.create_task(slow_enqueue())
    worker_mod._pending.add(task)
    task.add_done_callback(worker_mod._pending.discard)
    w = worker_mod.MirrorWorker.__new__(worker_mod.MirrorWorker)
    w._task = None
    await w.stop()
    assert done.is_set()
