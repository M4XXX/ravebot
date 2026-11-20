"""Real-SQL checks of bot/db/outbox.py, bot/db/mirror.py and a worker drain."""
import datetime

from bot.db import bookings, djs, mirror, outbox
from bot.db.events import EventDraft, insert_event
from bot.sync.payloads import payload_hash
from bot.sync.worker import MirrorWorker
from bot.timezone import local_today

A = 111111111111111111


async def _row(pool, entity, key):  # noqa: ANN001, ANN202
    return await pool.fetchrow(
        "SELECT * FROM notion_outbox WHERE entity = $1 AND entity_key = $2", entity, key
    )


async def test_enqueue_coalesces(pool) -> None:
    await outbox.enqueue(pool, "event", "u1")
    await outbox.failed(pool, "event", "u1", "boom")
    await outbox.enqueue(pool, "event", "u1", "delete", "page-1")
    row = await _row(pool, "event", "u1")
    assert (row["op"], row["notion_page_id"], row["attempts"], row["last_error"]) == (
        "delete", "page-1", 0, None
    )
    await outbox.enqueue(pool, "event", "u1")
    row = await _row(pool, "event", "u1")
    assert row["op"] == "upsert" and row["notion_page_id"] == "page-1"
    assert await pool.fetchval("SELECT count(*) FROM notion_outbox") == 1
    assert [r.entity_key for r in await outbox.due(pool, 10)] == ["u1"]


async def test_done_keeps_newer_enqueue(pool) -> None:
    await outbox.enqueue(pool, "slot", "u1:0")
    (first,) = await outbox.due(pool, 10)
    await outbox.enqueue(pool, "slot", "u1:0")  # changed again while the write ran
    await outbox.done(pool, "slot", "u1:0", first.enqueued_at)
    assert await _row(pool, "slot", "u1:0") is not None
    (second,) = await outbox.due(pool, 10)
    await outbox.done(pool, "slot", "u1:0", second.enqueued_at)
    assert await _row(pool, "slot", "u1:0") is None


async def test_failed_backs_off(pool) -> None:
    await outbox.enqueue(pool, "dj", "42")
    delays = []
    for _ in range(3):
        await outbox.failed(pool, "dj", "42", "boom")
        delays.append(await pool.fetchval(
            "SELECT extract(epoch FROM next_attempt_at - now()) FROM notion_outbox"
        ))
    assert [round(d) for d in delays] == [10, 20, 40]
    assert await outbox.due(pool, 10) == []
    await pool.execute("UPDATE notion_outbox SET attempts = 500")
    await outbox.failed(pool, "dj", "42", "still")
    capped = await pool.fetchval(
        "SELECT extract(epoch FROM next_attempt_at - now()) FROM notion_outbox"
    )
    assert round(capped) == 3600
    stats = await outbox.stats(pool)
    assert (stats.count, stats.last_error) == (1, "still") and stats.oldest is not None


async def test_due_hides_open_conflicts_only(pool) -> None:
    await outbox.enqueue(pool, "event", "u1")
    await outbox.enqueue(pool, "event", "u2")
    await pool.execute(
        "INSERT INTO sync_issues (kind, entity, entity_key, summary) VALUES"
        " ('conflict', 'event', 'u1', 'x'), ('warning', 'event', 'u2', 'y')"
    )
    assert [r.entity_key for r in await outbox.due(pool, 10)] == ["u2"]
    assert await mirror.open_issue_keys(pool) == {("event", "u1")}
    await pool.execute("UPDATE sync_issues SET resolved_at = now()")
    assert await mirror.open_issue_keys(pool) == set()
    assert len(await outbox.due(pool, 10)) == 2


async def test_mirror_round_trip(pool) -> None:
    payload = {"event_uid": "u1", "number": 1, "discord_id": A}
    await mirror.set_synced(pool, "slot", "u1:0", "page-1", payload)
    row = await mirror.get(pool, "slot", "u1:0")
    assert row.notion_page_id == "page-1" and row.synced_payload == payload
    assert row.synced_hash == payload_hash(payload) and row.synced_at is not None
    await mirror.set_synced(pool, "slot", "u1:0", "page-2", {**payload, "discord_id": None})
    assert (await mirror.get_all(pool, "slot"))["u1:0"].notion_page_id == "page-2"
    await mirror.forget(pool, "slot", "u1:0")
    assert await mirror.get(pool, "slot", "u1:0") is None


class _Notion:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def mirror_event(self, uid, payload, page_id):  # noqa: ANN001, ANN202
        self.calls.append(f"event {uid}")
        return "page-ev"

    async def mirror_dj(self, payload, page_id, *, logo_image, upload_logo, logo_url=None):  # noqa: ANN001, ANN202
        self.calls.append(f"dj {payload['discord_id']} logo={upload_logo}")
        return "page-dj"

    async def mirror_slot(self, payload, event_page_id, dj_page_id, page_id, *, event_label=None):  # noqa: ANN001, ANN202
        self.calls.append(f"slot {payload['number']} {event_page_id} {dj_page_id}")
        return f"page-sl-{payload['number']}"

    async def trash_page(self, page_id):  # noqa: ANN001, ANN202
        self.calls.append(f"trash {page_id}")


async def test_drain_against_postgres(pool) -> None:
    await djs.insert_dj(
        pool, discord_user_id=A, dj_name="DJ A", vrchat_name=None, logo_url=None,
        stream_url_primary=None, stream_url_backup=None, active_booking=True,
        logo_image=b"png",
    )
    day = local_today() + datetime.timedelta(days=3)
    await insert_event(
        pool,
        EventDraft(event_id="E1", date=day, start=datetime.time(20, 0),
                   slot_minutes=60, slot_count=2),
        uid="u1",
    )
    await bookings.acquire_slot_lock(pool, "E1", 1, A)
    for entity, key in (("slot", "u1:1"), ("slot", "u1:0"), ("dj", str(A)), ("event", "u1")):
        await outbox.enqueue(pool, entity, key)
    notion = _Notion()

    assert await MirrorWorker(pool, notion, None, rate=1e9).drain_once() == 4

    assert notion.calls == [
        "event u1", f"dj {A} logo=True", "slot 2 page-ev page-dj", "slot 1 page-ev None",
    ]
    assert (await outbox.stats(pool)).count == 0
    assert (await mirror.get(pool, "slot", "u1:1")).synced_payload == {
        "event_uid": "u1", "number": 2, "discord_id": A,
    }
    assert await pool.fetchval(
        "SELECT logo_synced FROM djs WHERE discord_user_id = $1", A
    ) is True
