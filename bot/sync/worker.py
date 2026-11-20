"""Background mirror worker: pushes queued Postgres changes to Notion.

Write paths call ``changed(ctx, entity, key)`` after their Postgres commit. It
queues the entity in the outbox in a background task and wakes the worker; no
Discord handler ever waits for the outbox or for Notion. The worker writes each
due entity from its current Postgres state (events and DJs before slots, since a
slot page needs both page ids), at most 3 Notion requests per second, and records
the written payload in ``notion_mirror``. Entities with an open conflict are
skipped until the conflict is decided.

``gate`` is held for each write (Notion request plus the mirror record). The sync
cycle holds it for its whole run, so the worker never records a newer state while
the cycle compares an older Notion snapshot against the mirror (the worker waits;
Discord handlers never take the gate).
"""
import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from bot.db import djs, mirror, outbox
from bot.notion.client import DjLogoUploadError
from bot.sync.botdata import botdata_row
from bot.sync.payloads import dj_payload, event_payload, slot_payload

__all__ = ["MirrorWorker", "changed"]

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
IDLE_SECONDS = 10
REQUESTS_PER_SECOND = 3.0
# Notion requests per write: mirror_* reads the page (alive check) and then writes it.
_WRITE_REQUESTS = 2
_LOGO_REQUESTS = 2  # file upload: create + send
_ERROR_LIMIT = 500

_ORDER = {"event": 0, "dj": 1, "slot": 2}

# changed() tasks, kept so they are not garbage collected before they finish.
_pending: set[asyncio.Task[None]] = set()


class _NotReady(Exception):
    """A slot cannot be written yet (its event page is missing)."""


class _RateLimiter:
    """Spaces Notion requests to ``rate`` per second (reservations in order)."""

    def __init__(
        self,
        rate: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    async def acquire(self, requests: int) -> None:
        now = self._clock()
        start = max(now, self._next)
        self._next = start + requests * self._interval
        if start > now:
            await self._sleep(start - now)


class MirrorWorker:
    def __init__(
        self,
        db: asyncpg.Pool,
        notion_client: Any,
        log_service: Any,
        *,
        rate: float = REQUESTS_PER_SECOND,
        name_resolver: Callable[[int], str | None] | None = None,
    ) -> None:
        self.db = db
        self.notion_client = notion_client
        self.log_service = log_service
        # Discord user id -> current Discord display name, for the "Discord name" column
        self._name_resolver = name_resolver
        self._limiter = _RateLimiter(rate)
        self._wake = asyncio.Event()
        # Held per write; the sync cycle holds it for its whole run (see module doc).
        self.gate = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_batch = 0

    # ---- lifecycle ----

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="notion-mirror-worker")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def notify(self) -> None:
        """Wake the loop now instead of after the idle wait. Never blocks."""
        self._wake.set()

    async def _run(self) -> None:
        more = False
        while True:
            if not more:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), IDLE_SECONDS)
            # Cleared before the drain: a change queued during it wakes the next pass.
            self._wake.clear()
            try:
                written = await self.drain_once()
                # A full batch with progress: more rows may be due, go on without waiting.
                more = written > 0 and self._last_batch >= BATCH_SIZE
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notion mirror drain failed")
                more = False

    # ---- drain ----

    async def drain_once(self) -> int:
        """Write the due outbox rows to Notion; returns how many succeeded."""
        rows = await outbox.due(self.db, BATCH_SIZE)
        self._last_batch = len(rows)
        if not rows:
            return 0
        stalled = await mirror.open_issue_keys(self.db)
        written = 0
        for row in sorted(rows, key=lambda r: (_ORDER.get(r.entity, 3), r.enqueued_at)):
            waited = self.gate.locked()
            async with self.gate:
                if waited:
                    # A sync cycle ran meanwhile and may have opened conflicts.
                    stalled = await mirror.open_issue_keys(self.db)
                if (row.entity, row.entity_key) in stalled:
                    continue
                if self.notion_client is None:
                    break
                if await self._write_one(row):
                    written += 1
        return written

    async def _write_one(self, row: outbox.OutboxRow) -> bool:
        try:
            await self._write(row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Notion mirror of %s %s failed", row.entity, row.entity_key, exc_info=True
            )
            await outbox.failed(
                self.db, row.entity, row.entity_key, (str(exc) or repr(exc))[:_ERROR_LIMIT]
            )
            return False
        await outbox.done(self.db, row.entity, row.entity_key, row.enqueued_at)
        return True

    async def _write(self, row: outbox.OutboxRow) -> None:
        if row.entity == "event":
            await self._sync_event(row.entity_key, row.op, row.notion_page_id)
        elif row.entity == "dj":
            await self._sync_dj(row.entity_key, row.op, row.notion_page_id)
        elif row.entity == "slot":
            await self._sync_slot(row.entity_key, row.op, row.notion_page_id)
        elif row.entity == "botdata":
            await self._sync_botdata(row.entity_key, row.op, row.notion_page_id)
        else:
            raise ValueError(f"unknown mirror entity: {row.entity}")

    async def _trash(self, entity: str, key: str, page_id: str | None) -> None:
        """Delete: trash the stored page (outbox page id, else mirror page id)."""
        if page_id is None:
            page_id = await self._page_id(entity, key)
        if page_id is not None:
            await self._limiter.acquire(1)
            await self.notion_client.trash_page(page_id)
        await mirror.forget(self.db, entity, key)

    async def _page_id(self, entity: str, key: str) -> str | None:
        known = await mirror.get(self.db, entity, key)
        return known.notion_page_id if known is not None else None

    async def _sync_event(
        self, uid: str, op: str = "upsert", page_id: str | None = None
    ) -> str | None:
        row = await self.db.fetchrow(
            "SELECT uid, event_id, date, start, slot_minutes, slot_count, status, genre, notes"
            " FROM events WHERE uid = $1",
            uid,
        )
        if op == "delete" or row is None:
            await self._trash("event", uid, page_id)
            return None
        payload = event_payload(dict(row))
        await self._limiter.acquire(_WRITE_REQUESTS)
        new_id = await self.notion_client.mirror_event(
            uid, payload, await self._page_id("event", uid)
        )
        await mirror.set_synced(self.db, "event", uid, new_id, payload)
        return new_id

    def _discord_name(self, discord_id: int) -> str | None:
        """The DJ's Discord name, or None (no resolver, not in the cache, guest)."""
        if self._name_resolver is None or discord_id <= 0:
            return None
        try:
            return self._name_resolver(discord_id)
        except Exception:
            logger.warning("Resolving the Discord name of %s failed", discord_id, exc_info=True)
            return None

    async def _sync_dj(
        self, key: str, op: str = "upsert", page_id: str | None = None
    ) -> str | None:
        row = await djs.get_dj_by_discord_id(self.db, int(key))
        if op == "delete" or row is None:
            await self._trash("dj", key, page_id)
            return None
        payload = dj_payload(dict(row))
        image = row["logo_image"]
        upload_logo = not row["logo_synced"]
        await self._limiter.acquire(
            _WRITE_REQUESTS + (_LOGO_REQUESTS if image is not None else 0)
        )
        try:
            new_id = await self.notion_client.mirror_dj(
                payload,
                await self._page_id("dj", key),
                logo_image=image,
                upload_logo=upload_logo,
                logo_url=row["logo_url"],
                discord_name=self._discord_name(int(key)),
            )
        except DjLogoUploadError as exc:
            # The row is written; keep it and retry the logo through the outbox backoff.
            await mirror.set_synced(self.db, "dj", key, exc.page_id, payload)
            raise
        await mirror.set_synced(self.db, "dj", key, new_id, payload)
        if upload_logo:
            await djs.mark_logo_synced_if_unchanged(self.db, int(key), image)
        return new_id

    async def _sync_botdata(
        self, key: str, op: str = "upsert", page_id: str | None = None
    ) -> None:
        current = None if op == "delete" else await botdata_row(self.db, key)
        if current is None:
            await self._trash("botdata", key, page_id)
            return
        table, row = current
        await self._limiter.acquire(_WRITE_REQUESTS)
        new_id = await self.notion_client.mirror_botdata(
            key, table, row, await self._page_id("botdata", key)
        )
        await mirror.set_synced(self.db, "botdata", key, new_id, row)

    async def _sync_slot(self, key: str, op: str = "upsert", page_id: str | None = None) -> None:
        uid, _, index_text = key.rpartition(":")
        slot_index = int(index_text)
        event = await self.db.fetchrow(
            "SELECT event_id, slot_count FROM events WHERE uid = $1", uid
        )
        if op == "delete" or event is None or slot_index >= event["slot_count"]:
            await self._trash("slot", key, page_id)
            return
        holder = await self.db.fetchval(
            "SELECT discord_user_id FROM slot_locks WHERE event_id = $1 AND slot_index = $2",
            uid,  # slot_locks is keyed by the event uid, not its title
            slot_index,
        )
        # A slot page needs its event's page (and its DJ's): write them first if missing.
        event_page = await self._page_id("event", uid) or await self._sync_event(uid)
        if event_page is None:
            raise _NotReady(f"event {uid} has no Notion page")
        dj_page = None
        if holder is not None:
            dj_key = str(holder)
            dj_page = await self._page_id("dj", dj_key) or await self._sync_dj(dj_key)
        payload = slot_payload(uid, slot_index, holder)
        await self._limiter.acquire(_WRITE_REQUESTS)
        new_id = await self.notion_client.mirror_slot(
            payload,
            event_page,
            dj_page,
            await self._page_id("slot", key),
            event_label=event["event_id"],
        )
        await mirror.set_synced(self.db, "slot", key, new_id, payload)


async def _enqueue_and_notify(
    worker: MirrorWorker, entity: str, key: str, op: str, notion_page_id: str | None
) -> None:
    await outbox.enqueue(worker.db, entity, key, op, notion_page_id)
    worker.notify()


def _finished(task: asyncio.Task[None]) -> None:
    _pending.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Queueing a Notion mirror write failed", exc_info=exc)


def changed(
    ctx: Any,
    entity: str,
    key: str,
    op: str = "upsert",
    notion_page_id: str | None = None,
) -> None:
    """Queue ``entity``/``key`` for the Notion mirror and return at once.

    No-op when Notion is disabled (``ctx.mirror`` is None). Call after the
    Postgres commit; a lost enqueue is repaired by the Notion poll.
    """
    worker = ctx.mirror
    if worker is None:
        return
    task = asyncio.create_task(_enqueue_and_notify(worker, entity, key, op, notion_page_id))
    _pending.add(task)
    task.add_done_callback(_finished)
