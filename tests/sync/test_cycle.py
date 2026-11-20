"""Tests for bot/sync/cycle.py: one Notion poll cycle, conflicts, warnings, 24 h rule.

Postgres rows, the mirror, sync_issues, config and the outbox are an in-memory store;
the real SQL is covered in tests/pg/test_issues.py and tests/pg/test_sync_cycle.py.
Intake is replaced by a recorder (its checks are covered in tests/sync/test_intake.py).
"""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db import config as config_mod
from bot.db import issues as issues_mod
from bot.db import mirror as mirror_mod
from bot.db.issues import IssueRow
from bot.db.mirror import MirrorRow
from bot.db.outbox import OutboxRow
from bot.sync import cycle as cycle_mod
from bot.sync import worker as worker_mod
from bot.sync.cycle import CycleReport, SyncCycle
from bot.sync.payloads import payload_hash
from bot.sync.worker import MirrorWorker
from tests.sync.pages import dj_page, event_page, slot_page, snapshot

NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.UTC)
A = 111111111111111111


def ev(title: str, **over) -> dict:
    payload = {
        "event_id": title, "date": "2026-12-01", "start": "20:00", "slot_minutes": 60,
        "slot_count": 3, "status": None, "genre": None, "notes": None,
    }
    payload.update(over)
    return payload


class Store:
    def __init__(self) -> None:
        self.pg: dict[str, dict[str, dict]] = {"event": {}, "dj": {}, "slot": {}}
        self.botdata: dict[str, tuple[str, dict]] = {}
        self.mirror: dict[tuple[str, str], MirrorRow] = {}
        self.issues: list[IssueRow] = []
        self.config: dict[str, str] = {}
        self.changed: list[tuple] = []
        self.queue: dict[tuple[str, str], OutboxRow] = {}

    # setup helpers
    def event(self, uid: str, payload: dict, page: str | None, synced: dict | None = None) -> None:
        self.pg["event"][uid] = payload
        for i in range(payload["slot_count"]):
            self.pg["slot"][f"{uid}:{i}"] = {"event_uid": uid, "number": i + 1, "discord_id": None}
        if page is not None:
            self.sync("event", uid, page, synced if synced is not None else payload)

    def sync(self, entity: str, key: str, page: str, payload: dict) -> None:
        self.mirror[(entity, key)] = MirrorRow(
            entity, key, page, payload_hash(payload), payload, NOW
        )

    def conflict(self, entity: str, key: str, *, age_hours: float = 0, notion=None) -> IssueRow:
        row = IssueRow(
            id=len(self.issues) + 1, kind="conflict", entity=entity, entity_key=key,
            summary=f"{key} changed on both sides", pg_payload=None, notion_payload=notion,
            created_at=NOW - datetime.timedelta(hours=age_hours), resolved_at=None,
            resolution=None,
        )
        self.issues.append(row)
        return row

    def open(self, kind: str | None = None) -> list[IssueRow]:
        return [i for i in self.issues if i.resolved_at is None and kind in (None, i.kind)]

    # pg
    async def load_pg(self, db, entity):  # noqa: ANN001, ANN202
        return dict(self.pg[entity])

    async def load_botdata(self, db):  # noqa: ANN001, ANN202
        return dict(self.botdata)

    # mirror
    async def get_all(self, db, entity):  # noqa: ANN001, ANN202
        return {k: r for (e, k), r in self.mirror.items() if e == entity}

    async def get(self, db, entity, key):  # noqa: ANN001, ANN202
        return self.mirror.get((entity, key))

    async def set_synced(self, db, entity, key, page_id, payload):  # noqa: ANN001, ANN202
        self.sync(entity, key, page_id, payload)

    async def forget(self, db, entity, key):  # noqa: ANN001, ANN202
        self.mirror.pop((entity, key), None)

    async def open_issue_keys(self, db):  # noqa: ANN001, ANN202
        return {(i.entity, i.entity_key) for i in self.open("conflict")}

    # issues
    async def open_conflict(self, db, entity, key, summary, pg_payload, notion_payload):  # noqa: ANN001, ANN202
        if (entity, key) in await self.open_issue_keys(db):
            return None
        row = IssueRow(
            len(self.issues) + 1, "conflict", entity, key, summary, pg_payload,
            notion_payload, NOW, None, None,
        )
        self.issues.append(row)
        return row.id

    async def add_warning(self, db, summary, entity=None, key=None, notion_payload=None):  # noqa: ANN001, ANN202
        row = IssueRow(
            len(self.issues) + 1, "warning", entity, key, summary, None, notion_payload,
            NOW, None, None,
        )
        self.issues.append(row)
        return row.id

    async def list_open(self, db):  # noqa: ANN001, ANN202
        return self.open()

    async def resolve(self, db, issue_id, resolution):  # noqa: ANN001, ANN202
        for n, row in enumerate(self.issues):
            if row.id == issue_id and row.resolved_at is None:
                self.issues[n] = row = IssueRow(
                    **{**row.__dict__, "resolved_at": NOW, "resolution": resolution}
                )
                return row
        return None

    async def expired_conflicts(self, db, older_than):  # noqa: ANN001, ANN202
        return [i for i in self.open("conflict") if NOW - i.created_at > older_than]

    # config
    async def get_value(self, db, guild_id, key):  # noqa: ANN001, ANN202
        assert guild_id == 0
        return self.config.get(key)

    async def set_value(self, db, guild_id, key, value):  # noqa: ANN001, ANN202
        assert guild_id == 0
        self.config[key] = value

    # changed() + outbox
    def record_changed(self, ctx, entity, key, op="upsert", notion_page_id=None):  # noqa: ANN001, ANN202
        self.changed.append((entity, key, op))
        self.queue[(entity, key)] = OutboxRow(
            entity, key, op, notion_page_id, NOW + datetime.timedelta(seconds=len(self.queue)),
            0, NOW, None,
        )

    async def due(self, db, limit):  # noqa: ANN001, ANN202
        return list(self.queue.values())[:limit]

    async def done(self, db, entity, key, enqueued_at):  # noqa: ANN001, ANN202
        self.queue.pop((entity, key), None)

    async def failed(self, db, entity, key, error):  # noqa: ANN001, ANN202
        raise AssertionError(f"{entity} {key} failed: {error}")


class FakeIntake:
    """Records the changes handed to Intake.apply; rejects the keys in ``reject``."""

    instances: list["FakeIntake"] = []
    reject: dict = {}
    on_apply = None

    def __init__(self, ctx, bot) -> None:  # noqa: ANN001
        self.applied: list = []
        self.last_reasons: list[str] = []
        self.flushed = 0
        FakeIntake.instances.append(self)

    async def apply(self, change):  # noqa: ANN001, ANN201
        self.applied.append(change)
        reason = FakeIntake.reject.get((change.entity, change.key))
        if reason:
            self.last_reasons = [reason]
            return "rejected"
        if FakeIntake.on_apply is not None:
            await FakeIntake.on_apply(change)
        return "applied"

    async def flush_log(self) -> None:
        self.flushed += 1

    @classmethod
    def all_applied(cls) -> list:
        return [c for i in cls.instances for c in i.applied]


@pytest.fixture
def store(monkeypatch):
    s = Store()
    for name in ("get_all", "get", "set_synced", "open_issue_keys"):
        monkeypatch.setattr(mirror_mod, name, getattr(s, name))
    for name in ("open_conflict", "add_warning", "list_open", "resolve", "expired_conflicts"):
        monkeypatch.setattr(issues_mod, name, getattr(s, name))
    monkeypatch.setattr(config_mod, "get_value", s.get_value)
    monkeypatch.setattr(config_mod, "set_value", s.set_value)
    monkeypatch.setattr(cycle_mod, "_load_pg", s.load_pg)
    monkeypatch.setattr(cycle_mod, "botdata_rows", s.load_botdata)
    monkeypatch.setattr(mirror_mod, "forget", s.forget, raising=True)
    monkeypatch.setattr(cycle_mod, "changed", s.record_changed)
    FakeIntake.instances = []
    FakeIntake.reject = {}
    FakeIntake.on_apply = None
    monkeypatch.setattr(cycle_mod, "Intake", FakeIntake)
    return s


def make_cycle(notion_snapshot=None, *, read_error=None):  # noqa: ANN001, ANN201
    client = MagicMock()
    client.read_all = AsyncMock(return_value=notion_snapshot, side_effect=read_error)
    log = MagicMock()
    log.log_attention = AsyncMock()
    ctx = SimpleNamespace(
        db=object(), notion_client=client, log_service=log, maintenance=None,
        mirror=MagicMock(),
    )
    return SyncCycle(ctx, bot=MagicMock()), ctx


def attention_titles(ctx) -> list[str]:  # noqa: ANN001
    return [c.args[0] for c in ctx.log_service.log_attention.await_args_list]


async def test_conflict_stalls_only_its_entity(store, monkeypatch) -> None:
    # Both events are synced; a resync queued them both in the outbox.
    store.event("ua", ev("Event A"), "pa")
    store.event("ub", ev("Event B"), "pb")
    for uid, page in (("ua", "pa"), ("ub", "pb")):
        for i in range(3):
            key = f"{uid}:{i}"
            store.sync("slot", key, f"{page}-s{i}", store.pg["slot"][key])
    store.record_changed(None, "event", "ua")
    store.record_changed(None, "event", "ub")
    store.changed.clear()
    # A: changed in the bot (genre) and in Notion (start) -> conflict.
    store.pg["event"]["ua"] = ev("Event A", genre="Techno")
    # B: changed in Notion only -> applied.
    pages = [
        event_page("pa", "Event A", start="21:00"),
        event_page("pb", "Event B", genre="Trance"),
    ]
    slots = [slot_page(f"{p}-s{i}", p, i + 1) for p in ("pa", "pb") for i in range(3)]
    cycle, ctx = make_cycle(snapshot(pages, slots))

    report = await cycle.run()

    assert report == CycleReport(applied=1, pushed=0, conflicts=1, warnings=0)
    (conflict,) = store.open("conflict")
    assert (conflict.entity, conflict.entity_key) == ("event", "ua")
    assert conflict.pg_payload["genre"] == "Techno"
    assert conflict.notion_payload["start"] == "21:00"
    (applied,) = FakeIntake.all_applied()
    assert (applied.kind, applied.key, applied.notion["genre"]) == ("apply", "ub", "Trance")
    assert attention_titles(ctx) == [cycle_mod.texts.LOG_SYNC_CONFLICT_TITLE]
    what, todo = ctx.log_service.log_attention.await_args.args[1:]
    assert "Techno" in what and "21:00" in what
    assert "choose on the admin page" in todo

    # The worker drains B's outbox row; A's stays queued until the conflict is decided.
    written: list[str] = []
    worker = MirrorWorker(object(), MagicMock(), None)
    monkeypatch.setattr(
        worker_mod, "outbox", SimpleNamespace(due=store.due, done=store.done, failed=store.failed)
    )

    async def write(row):  # noqa: ANN001, ANN202
        written.append(row.entity_key)

    monkeypatch.setattr(worker, "_write", write)
    assert await worker.drain_once() == 1
    assert written == ["ub"]
    assert list(store.queue) == [("event", "ua")]

    # The next cycle leaves A alone (no second conflict, no log).
    await cycle.run()
    assert len(store.open("conflict")) == 1
    assert len(attention_titles(ctx)) == 1


async def test_conflict_expires_after_24h_postgres_wins(store) -> None:
    store.event("ua", ev("Event A", genre="Techno"), "pa", synced=ev("Event A"))
    store.conflict("event", "ua", age_hours=25)
    young = store.conflict("event", "ux", age_hours=1)
    # Notion still has its own edit; the bot's version must win, not a new conflict.
    cycle, ctx = make_cycle(snapshot([event_page("pa", "Event A", start="21:00")]))

    report = await cycle.run()

    assert [(i.entity_key, i.resolution) for i in store.issues if i.resolved_at] == [
        ("ua", "expired")
    ]
    assert [i.id for i in store.open("conflict")] == [young.id]
    assert ("event", "ua", "upsert") in store.changed
    assert report.conflicts == 0
    assert FakeIntake.all_applied() == []
    assert cycle_mod.texts.LOG_SYNC_EXPIRED_TITLE == "Postgres kept after 24 h"
    assert attention_titles(ctx) == ["Postgres kept after 24 h"]


async def test_resolve_keep_notion_applies(store) -> None:
    store.event("ua", ev("Event A", genre="Techno"), "pa", synced=ev("Event A"))
    notion = ev("Event A", start="21:00")
    issue = store.conflict("event", "ua", notion=notion)
    cycle, _ = make_cycle()

    result = await cycle.resolve_issue(issue.id, "notion")

    assert result.ok
    (change,) = FakeIntake.all_applied()
    assert (change.kind, change.entity, change.key) == ("apply", "event", "ua")
    assert change.page_id == "pa"
    assert change.notion == notion
    assert change.pg == ev("Event A", genre="Techno")
    assert store.issues[0].resolution == "notion"
    assert FakeIntake.instances[0].flushed == 1


async def test_resolve_keep_notion_rejected_stays_open(store) -> None:
    store.event("ua", ev("Event A"), "pa")
    issue = store.conflict("event", "ua", notion=ev("Event A", date="2020-01-01"))
    FakeIntake.reject = {("event", "ua"): "The date is in the past."}
    cycle, _ = make_cycle()

    result = await cycle.resolve_issue(issue.id, "notion")

    assert not result.ok and "The date is in the past." in result.message
    assert store.open("conflict") == [issue]


async def test_resolve_keep_postgres_and_resync(store) -> None:
    store.event("ua", ev("Event A"), "pa")
    conflict = store.conflict("event", "ua")
    warning_id = await store.add_warning(None, "page deleted", "event", "ua")
    cycle, _ = make_cycle()

    assert (await cycle.resolve_issue(warning_id, "notion")).ok is False  # wrong choice
    assert (await cycle.resolve_issue(conflict.id, "postgres")).ok
    assert (await cycle.resolve_issue(warning_id, "resynced")).ok
    assert (await cycle.resolve_issue(warning_id, "resynced")).ok is False  # closed
    assert store.changed == [("event", "ua", "upsert"), ("event", "ua", "upsert")]
    assert store.open() == []


async def test_deleted_page_warns_and_keeps_pg(store) -> None:
    store.event("ua", ev("Event A"), "pa")
    store.event("ub", ev("Event B"), "pb")
    cycle, ctx = make_cycle(snapshot([event_page("pb", "Event B")]))

    report = await cycle.run()

    assert report.warnings == 1
    (warning,) = store.open("warning")
    assert (warning.entity, warning.entity_key) == ("event", "ua")
    assert "Event A" in warning.summary
    assert store.pg["event"]["ua"] == ev("Event A")
    assert not [c for c in store.changed if c[0] == "event"]
    assert FakeIntake.all_applied() == []
    assert attention_titles(ctx) == [cycle_mod.texts.LOG_SYNC_WARNING_TITLE]

    # Still deleted on the next poll: no second warning, no second log entry.
    assert (await cycle.run()).warnings == 0
    assert len(store.open("warning")) == 1
    assert len(attention_titles(ctx)) == 1


async def test_rejected_notion_edit_warns(store) -> None:
    store.event("ua", ev("Event A"), "pa")
    FakeIntake.reject = {("event", "ua"): "Slots cannot go below 3: <@1> booked."}
    cycle, ctx = make_cycle(snapshot([event_page("pa", "Event A", slots=2)]))

    report = await cycle.run()

    assert (report.applied, report.conflicts, report.warnings) == (0, 0, 1)
    (warning,) = store.open("warning")
    assert "Slots cannot go below 3" in warning.summary
    what = ctx.log_service.log_attention.await_args.args[1]
    assert "<@1>" not in what  # Notion and Postgres text is escaped before posting


async def test_failed_notion_read_changes_nothing(store) -> None:
    store.event("ua", ev("Event A"), "pa")
    store.config["sync_last_run"] = "before"
    cycle, ctx = make_cycle(read_error=RuntimeError("Notion is down"))

    report = await cycle.run()

    assert report == CycleReport(0, 0, 0, 0)
    assert store.changed == [] and store.issues == []
    assert FakeIntake.all_applied() == []
    assert "Notion is down" in store.config["sync_last_error"]
    assert store.config["sync_last_run"] == "before"
    assert attention_titles(ctx) == [cycle_mod.texts.LOG_SYNC_READ_FAILED_TITLE]
    await cycle.run()  # still failing: no second log entry
    assert len(attention_titles(ctx)) == 1


async def test_success_records_last_run(store) -> None:
    store.config["sync_last_error"] = "old"
    cycle, _ = make_cycle(snapshot())
    await cycle.run()
    assert store.config["sync_last_error"] == ""
    assert datetime.datetime.fromisoformat(store.config["sync_last_run"]).tzinfo is not None


async def test_cycle_skipped_in_maintenance(store) -> None:
    store.conflict("event", "ua", age_hours=30)
    cycle, ctx = make_cycle(snapshot())
    ctx.maintenance = "restoring"

    assert await cycle.run() == CycleReport(0, 0, 0, 0)
    ctx.notion_client.read_all.assert_not_awaited()
    assert store.open("conflict") and store.changed == []


async def test_new_notion_event_slots_resolve_in_same_cycle(store) -> None:
    # A hand-made event with two hand-made slot pages (slot 1 booked, slot 2 free).
    store.pg["dj"][str(A)] = {
        "discord_id": A, "dj_name": "DJ A", "vrchat_name": None, "link": None, "alt_link": None,
    }
    store.sync("dj", str(A), "pdj", store.pg["dj"][str(A)])

    async def create(change):  # noqa: ANN001, ANN202
        if change.entity == "event":
            store.event("new", change.notion, change.page_id)

    FakeIntake.on_apply = create
    pages = [event_page("pn", "Hand Event")]
    slots = [slot_page("ps1", "pn", 1, "pdj"), slot_page("ps2", "pn", 2)]
    cycle, _ = make_cycle(snapshot(pages, slots, [dj_page("pdj", "DJ A", A)]))

    report = await cycle.run()

    kinds = [(c.entity, c.kind, c.key) for c in FakeIntake.all_applied()]
    assert kinds == [("event", "new_in_notion", None), ("slot", "new_in_notion", "new:0")]
    booked = FakeIntake.all_applied()[1]
    assert booked.notion == {"event_uid": "new", "number": 1, "discord_id": A}
    assert booked.page_id == "ps1"
    assert store.mirror[("slot", "new:1")].notion_page_id == "ps2"  # adopted as is
    assert store.changed == [("slot", "new:2", "upsert")]  # only the missing page is pushed
    assert report.warnings == 0 and report.applied == 2


async def test_resync_all_enqueues_everything(store, monkeypatch) -> None:
    store.event("ua", ev("Event A"), "pa")
    store.pg["dj"]["42"] = {"discord_id": 42}
    enqueued: list[tuple[str, str]] = []

    async def enqueue(db, entity, key, op="upsert", notion_page_id=None):  # noqa: ANN001, ANN202
        enqueued.append((entity, key))

    monkeypatch.setattr(cycle_mod.outbox, "enqueue", enqueue)
    cycle, ctx = make_cycle()

    assert await cycle.resync_all() == 5
    assert enqueued[:2] == [("event", "ua"), ("dj", "42")]
    ctx.mirror.notify.assert_called_once()
    ctx.mirror = None
    assert await cycle.resync_all() == 0


async def test_exclusive_waits_for_running_cycle(store, monkeypatch) -> None:
    import asyncio

    cycle, _ctx = make_cycle()
    entered = []

    async def hold() -> None:
        async with cycle.exclusive():
            entered.append(1)

    async with cycle._lock:
        task = asyncio.create_task(hold())
        await asyncio.sleep(0.05)
        assert not entered
    await task
    assert entered == [1]
    monkeypatch.setattr(cycle_mod, "_load_pg", AsyncMock(return_value={}))
    async with cycle.exclusive():  # resync_all must not take the lock again
        assert await asyncio.wait_for(cycle.resync_all(), 1) == 0


async def test_worker_write_during_read_waits_for_cycle(store, monkeypatch) -> None:
    """C1: a booking the worker mirrors while the cycle reads Notion is not undone."""
    import asyncio

    store.event("u1", ev("Rave"), "pe1")
    store.pg["dj"][str(A)] = {"discord_id": A, "dj_name": "Alpha", "vrchat_name": None,
                              "link": "https://a.example", "alt_link": None}
    store.sync("dj", str(A), "pd1", store.pg["dj"][str(A)])
    for i in range(3):
        store.sync("slot", f"u1:{i}", f"ps{i}", store.pg["slot"][f"u1:{i}"])
    # Notion snapshot taken before the booking: slot 1 free.
    snap = snapshot(
        events=[event_page("pe1", "Rave")],
        slots=[slot_page(f"ps{i}", "pe1", i + 1) for i in range(3)],
        djs=[dj_page("pd1", "Alpha", A, link="https://a.example")],
    )
    cycle, ctx = make_cycle(snap)
    worker = MirrorWorker(object(), MagicMock(), None)
    monkeypatch.setattr(
        worker_mod, "outbox", SimpleNamespace(due=store.due, done=store.done, failed=store.failed)
    )
    monkeypatch.setattr(worker_mod.mirror, "open_issue_keys", store.open_issue_keys)

    async def write(row):  # noqa: ANN001, ANN202
        # The worker pushes the booking to Notion and records it as synced.
        store.sync("slot", row.entity_key, "ps0", store.pg["slot"][row.entity_key])

    monkeypatch.setattr(worker, "_write", write)
    ctx.mirror = worker
    orig = ctx.notion_client.read_all
    drains: list[asyncio.Task] = []

    async def read_then_book():  # noqa: ANN202
        result = await orig()
        # During the read: a DJ books slot 1 and the worker drains the outbox.
        store.pg["slot"]["u1:0"] = {"event_uid": "u1", "number": 1, "discord_id": A}
        store.record_changed(None, "slot", "u1:0")
        drains.append(asyncio.create_task(worker.drain_once()))
        await asyncio.sleep(0.01)
        return result

    ctx.notion_client.read_all = read_then_book
    await cycle.run()
    await drains[0]

    applied = [(c.kind, c.key, c.notion) for c in FakeIntake.all_applied()]
    assert not applied, applied
    assert store.mirror[("slot", "u1:0")].synced_payload["discord_id"] == A
