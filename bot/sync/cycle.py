"""One Notion sync cycle: read Notion, compare with Postgres, act per entity.

Runs inside the Notion poll (bot/cogs/events.py), never during maintenance. Order:

1. Conflicts older than 24 h are closed as "expired": Postgres wins, the entity is
   queued for Notion and the admin log says so.
2. ``read_all()``. A failed read changes nothing; the error is stored and logged once.
3. Events, then DJs, then slots (R15: the slot pass sees events and DJs created from
   Notion in this cycle). Per entity the planner (bot/sync/plan.py) decides:
   ``push`` -> outbox; ``apply`` / ``new_in_notion`` -> Intake (a rejection is a
   warning); ``conflict`` -> one open conflict, which stalls only that entity;
   ``deleted_in_notion`` / ``key_changed`` / ``mass_change`` -> warning, nothing
   applied; ``agree`` -> recorded as synced.

After them the "Bot data" pass pushes bot tables to the read-only mirror: it never applies
and never conflicts, a hand edit is overwritten and a page without a row is trashed.

Warnings are not repeated while an identical one is still open. Every database call
uses its own short-lived pool connection; no row lock is held across a Notion call.
``sync_last_run`` and ``sync_last_error`` are stored under config guild 0.
"""
import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from bot.db import config as config_db
from bot.db import issues, mirror, outbox
from bot.db.issues import IssueRow
from bot.db.mirror import MirrorRow
from bot.notion.models import (
    PROP_DJ_NAME,
    PROP_EVENT_BOT_ID,
    PROP_SLOT_DJ,
    _relation_ids,
    _rich,
    _title,
)
from bot.results import ActionResult
from bot.sync.botdata import botdata_rows, is_oversized_page, parse_botdata_page
from bot.sync.intake import Intake
from bot.sync.payloads import (
    PROP_DJ_ALT_LINK,
    PROP_DJ_LINK,
    PROP_DJ_VRCHAT_NAME,
    _url,
    dj_payload,
    event_payload,
    notion_dj_payload,
    notion_event_payload,
    notion_slot_payload,
    payload_hash,
    slot_payload,
)
from bot.sync.plan import Change, Side, plan
from bot.sync.worker import changed
from bot.ui import texts

__all__ = ["CONFLICT_TTL", "CycleReport", "SyncCycle"]

logger = logging.getLogger(__name__)

CONFIG_GUILD_ID = 0
LAST_RUN_KEY = "sync_last_run"
LAST_ERROR_KEY = "sync_last_error"
CONFLICT_TTL = datetime.timedelta(hours=24)
_ERROR_LIMIT = 500

_PLURAL = {"event": "events", "dj": "DJs", "slot": "slots"}
_KEY_FIELD = {"event": "title", "dj": "Discord ID", "slot": "event or slot number"}
_FIELD_LABELS = {
    "event_id": "title", "date": "date", "start": "start", "slot_minutes": "slot length",
    "slot_count": "slots", "status": "status", "genre": "genre", "notes": "notes",
    "dj_name": "name", "vrchat_name": "VRChat name", "link": "stream link",
    "alt_link": "backup link", "event_uid": "event", "number": "slot number",
}


@dataclass(frozen=True)
class CycleReport:
    applied: int
    pushed: int
    conflicts: int
    warnings: int


class _Stopped(Exception):
    """Maintenance started during the cycle: stop without touching anything else."""


@dataclass
class _Counts:
    applied: int = 0
    pushed: int = 0
    conflicts: int = 0
    warnings: int = 0

    def report(self) -> CycleReport:
        return CycleReport(self.applied, self.pushed, self.conflicts, self.warnings)


async def _load_pg(db: asyncpg.Pool, entity: str) -> dict[str, dict[str, Any]]:
    """Canonical payloads of every Postgres record of ``entity``, by mirror key."""
    if entity == "event":
        rows = await db.fetch(
            "SELECT uid, event_id, date, start, slot_minutes, slot_count, status, genre, notes"
            " FROM events ORDER BY date, start, uid"
        )
        return {r["uid"]: event_payload(r) for r in rows}
    if entity == "dj":
        rows = await db.fetch(
            "SELECT discord_user_id, dj_name, vrchat_name, stream_url_primary,"
            " stream_url_backup FROM djs ORDER BY discord_user_id"
        )
        return {str(r["discord_user_id"]): dj_payload(r) for r in rows}
    if entity == "slot":
        rows = await db.fetch(
            "SELECT s.event_id, s.slot_index, s.discord_user_id FROM slot_locks s"
            " JOIN events e ON e.uid = s.event_id AND s.slot_index < e.slot_count"
            " ORDER BY s.event_id, s.slot_index"
        )
        return {
            f"{r['event_id']}:{r['slot_index']}": slot_payload(
                r["event_id"], r["slot_index"], r["discord_user_id"]
            )
            for r in rows
        }
    raise ValueError(f"unknown sync entity: {entity}")


def _notion_dj(page: dict[str, Any]) -> dict[str, Any] | None:
    """DJ page payload; a page with a name but no Discord ID gets discord_id None."""
    payload = notion_dj_payload(page)
    if payload is not None:
        return payload
    name = _title(page, PROP_DJ_NAME)
    if not name:
        return None
    return {
        "discord_id": None,
        "dj_name": name,
        "vrchat_name": _rich(page, PROP_DJ_VRCHAT_NAME),
        "link": _url(page, PROP_DJ_LINK),
        "alt_link": _url(page, PROP_DJ_ALT_LINK),
    }


def _match(
    pages: list[dict[str, Any]],
    rows: dict[str, MirrorRow],
    parse: Callable[[dict[str, Any]], dict[str, Any] | None],
    extra: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[Side, list[dict[str, Any]], list[str]]:
    """Notion pages by mirror key (matched by the mirror's page id).

    Returns the matched side, the unmatched readable pages (with ``_page_id`` and
    ``extra`` fields) and the keys whose page cannot be read.
    """
    key_by_page = {r.notion_page_id: k for k, r in rows.items() if r.notion_page_id}
    payloads: dict[str, dict[str, Any]] = {}
    page_ids: dict[str, str] = {}
    unmatched: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for page in pages:
        page_id = str(page.get("id") or "")
        payload = parse(page)
        key = key_by_page.get(page_id)
        if key is not None:
            if payload is None:
                unreadable.append(key)
            else:
                payloads[key] = payload
                page_ids[key] = page_id
        elif payload is not None:
            unmatched.append({**payload, "_page_id": page_id, **(extra(page) if extra else {})})
    return Side(payloads, page_ids), unmatched, unreadable


def _strip(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if not k.startswith("_")}


class SyncCycle:
    def __init__(self, ctx: Any, bot: Any) -> None:
        self._ctx = ctx
        self._bot = bot
        # run and resolve_issue never interleave (admin page vs poll)
        self._lock = asyncio.Lock()
        self._counts = _Counts()
        self._titles: dict[str, str] = {}
        self._dj_names: dict[int, str] = {}
        self._known_warnings: set[tuple[str | None, str | None, str]] = set()
        self._stalled: set[tuple[str, str]] = set()

    @property
    def _db(self) -> asyncpg.Pool:
        return self._ctx.db

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Wait for a running cycle or admin decision, then keep them out until exit.

        For maintenance jobs (backup import). Do not call run, resolve_issue or any
        other method that takes the lock while holding it; resync_all does not.
        """
        async with self._lock:
            yield

    # ---- cycle ----

    async def run(self) -> CycleReport:
        """One cycle; never raises. Skipped during maintenance or without Notion."""
        if self._ctx.maintenance or self._ctx.notion_client is None or self._db is None:
            return CycleReport(0, 0, 0, 0)
        async with self._lock:
            self._counts = _Counts()
            if self._ctx.maintenance:
                return self._counts.report()
            try:
                await self._run()
            except _Stopped:
                logger.info("Notion sync cycle stopped: maintenance started")
            except Exception as exc:
                logger.exception("Notion sync cycle failed")
                try:
                    await self._record_error(str(exc) or repr(exc), post=False)
                except Exception:
                    logger.warning("Storing the sync error failed", exc_info=True)
            return self._counts.report()

    async def _run(self) -> None:
        expired = await self._expire()
        try:
            snapshot = await self._ctx.notion_client.read_all()
        except Exception as exc:
            logger.warning("Reading Notion for the sync failed: %s", exc)
            await self._record_error(str(exc) or repr(exc), post=True)
            return
        self._known_warnings = {
            (i.entity, i.entity_key, i.summary)
            for i in await issues.list_open(self._db)
            if i.kind == "warning"
        }
        self._stalled = await mirror.open_issue_keys(self._db) | expired
        intake = Intake(self._ctx, self._bot)
        try:
            await self._event_pass(snapshot.events, intake)
            await self._dj_pass(snapshot.djs, intake)
            await self._slot_pass(snapshot.slots, intake)
            await self._botdata_pass(snapshot.botdata)
        finally:
            await intake.flush_log()
        now = datetime.datetime.now(datetime.UTC).isoformat()
        await config_db.set_value(self._db, CONFIG_GUILD_ID, LAST_RUN_KEY, now)
        await config_db.set_value(self._db, CONFIG_GUILD_ID, LAST_ERROR_KEY, "")

    async def _expire(self) -> set[tuple[str, str]]:
        """Close conflicts older than 24 h: Postgres wins. Returns their entities."""
        done: set[tuple[str, str]] = set()
        for row in await issues.expired_conflicts(self._db, CONFLICT_TTL):
            if await issues.resolve(self._db, row.id, "expired") is None:
                continue
            if row.entity and row.entity_key:
                changed(self._ctx, row.entity, row.entity_key)
                done.add((row.entity, row.entity_key))
            await self._attention(
                texts.LOG_SYNC_EXPIRED_TITLE,
                texts.sync_expired_text(row.summary),
                texts.LOG_NO_MANUAL_ACTION,
            )
        return done

    async def _record_error(self, message: str, *, post: bool) -> None:
        """Store the error; post it once (not again while it keeps failing)."""
        previous = await config_db.get_value(self._db, CONFIG_GUILD_ID, LAST_ERROR_KEY)
        await config_db.set_value(
            self._db, CONFIG_GUILD_ID, LAST_ERROR_KEY, message[:_ERROR_LIMIT]
        )
        if post and not previous:
            await self._attention(
                texts.LOG_SYNC_READ_FAILED_TITLE,
                texts.sync_read_failed_text(message[:_ERROR_LIMIT]),
                texts.SYNC_READ_FAILED_TODO,
            )

    # ---- passes ----

    async def _event_pass(self, pages: list[dict[str, Any]], intake: Intake) -> None:
        pg = await _load_pg(self._db, "event")
        self._titles = {k: p["event_id"] for k, p in pg.items()}
        rows = await mirror.get_all(self._db, "event")
        side, unmatched, unreadable = _match(
            pages,
            rows,
            notion_event_payload,
            lambda page: {"_bot_id": _rich(page, PROP_EVENT_BOT_ID)},
        )
        await self._plan("event", pg, side, rows, unmatched, unreadable, intake)

    async def _dj_pass(self, pages: list[dict[str, Any]], intake: Intake) -> None:
        pg = await _load_pg(self._db, "dj")
        self._dj_names = {p["discord_id"]: p["dj_name"] for p in pg.values()}
        rows = await mirror.get_all(self._db, "dj")
        side, unmatched, unreadable = _match(pages, rows, _notion_dj)
        await self._plan("dj", pg, side, rows, unmatched, unreadable, intake)

    async def _slot_pass(self, pages: list[dict[str, Any]], intake: Intake) -> None:
        # Fresh maps: events and DJs created from Notion in this cycle resolve (R15).
        self._titles = {
            k: p["event_id"] for k, p in (await _load_pg(self._db, "event")).items()
        }
        event_uid_by_page = {
            r.notion_page_id: k
            for k, r in (await mirror.get_all(self._db, "event")).items()
            if r.notion_page_id
        }
        dj_by_page: dict[str, int | None] = {
            r.notion_page_id: int(k)
            for k, r in (await mirror.get_all(self._db, "dj")).items()
            if r.notion_page_id
        }

        def parse(page: dict[str, Any]) -> dict[str, Any] | None:
            dj_pages = _relation_ids(page, PROP_SLOT_DJ)
            if dj_pages and dj_pages[0] not in dj_by_page:
                return None  # a DJ page the bot does not know: never read it as "free"
            return notion_slot_payload(page, event_uid_by_page, dj_by_page)

        pg = await _load_pg(self._db, "slot")
        rows = await mirror.get_all(self._db, "slot")
        side, unmatched, unreadable = _match(pages, rows, parse)
        unmatched, adopted = await self._adopt_slots(pg, rows, unmatched, intake)
        await self._plan("slot", pg, side, rows, unmatched, unreadable, intake, adopted)

    async def _botdata_pass(self, pages: list[dict[str, Any]]) -> None:
        """Push-only: queue rows that differ from Notion, trash pages without a row."""
        pg = await botdata_rows(self._db)
        rows = await mirror.get_all(self._db, "botdata")
        key_by_page = {r.notion_page_id: k for k, r in rows.items() if r.notion_page_id}
        live: dict[str, dict[str, Any] | None] = {}  # key -> (parsed) Notion row payload
        for page in pages:
            if self._ctx.maintenance:
                raise _Stopped
            page_id = str(page.get("id") or "")
            parsed = parse_botdata_page(page)
            key = key_by_page.get(page_id)
            if key is None or key not in pg:
                # No row in Postgres, or a page the mirror does not know: trash it. A stray
                # page gets its own outbox key so it never replaces a row's own write.
                changed(self._ctx, "botdata", key or f"stray:{page_id}", "delete", page_id)
                self._counts.pushed += 1
            elif parsed is None or parsed[0] != key:
                live[key] = None  # hand-edited key or unreadable page: overwrite
            elif parsed[2] is None and is_oversized_page(page):
                # Left out on purpose (too large): the hash check below detects changes.
                live[key] = rows[key].synced_payload
            else:
                live[key] = parsed[2]
        for key, (_, row) in pg.items():
            if self._ctx.maintenance:
                raise _Stopped
            known = rows.get(key)
            if (
                known is None
                or known.notion_page_id is None
                or key not in live
                or known.synced_hash != payload_hash(row)
                or live[key] != known.synced_payload
            ):
                changed(self._ctx, "botdata", key)
                self._counts.pushed += 1
        page_ids = {str(p.get("id") or "") for p in pages}
        for key in rows.keys() - pg.keys():
            if rows[key].notion_page_id not in page_ids:
                await mirror.forget(self._db, "botdata", key)  # page and row both gone

    async def _adopt_slots(
        self,
        pg: dict[str, dict[str, Any]],
        rows: dict[str, MirrorRow],
        unmatched: list[dict[str, Any]],
        intake: Intake,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Hand-made slot pages of slots that have no Notion page yet (R15).

        One page per such slot is adopted: same holder -> recorded as synced; a DJ
        on a free slot -> a booking through Intake; another holder -> Postgres wins
        and the page is overwritten. Returns the rest and the adopted keys.
        """
        candidates: dict[str, list[dict[str, Any]]] = {}
        for page in unmatched:
            key = f"{page['event_uid']}:{page['number'] - 1}"
            row = rows.get(key)
            has_page = row is not None and row.notion_page_id is not None
            if key in pg and not has_page and ("slot", key) not in self._stalled:
                candidates.setdefault(key, []).append(page)
        adopted = {key: pages[0] for key, pages in candidates.items() if len(pages) == 1}
        rest = [p for p in unmatched if not any(p is a for a in adopted.values())]
        for key, page in adopted.items():
            payload, page_id, current = _strip(page), page["_page_id"], pg[key]
            if payload == current:
                await mirror.set_synced(self._db, "slot", key, page_id, payload)
            elif current["discord_id"] is None:
                await self._intake(
                    Change("new_in_notion", "slot", key, current, payload, page_id), intake
                )
            else:
                await mirror.set_synced(self._db, "slot", key, page_id, payload)
                changed(self._ctx, "slot", key)
                self._counts.pushed += 1
        return rest, set(adopted)

    async def _plan(
        self,
        entity: str,
        pg: dict[str, dict[str, Any]],
        side: Side,
        rows: dict[str, MirrorRow],
        unmatched: list[dict[str, Any]],
        unreadable: list[str],
        intake: Intake,
        skip: set[str] | None = None,
    ) -> None:
        for key in unreadable:
            if (entity, key) not in self._stalled:
                await self._warn(
                    entity,
                    key,
                    texts.sync_unreadable_summary(self._label(entity, key, pg.get(key), None)),
                    texts.SYNC_RESYNC_TODO,
                )
        open_keys = {k for e, k in self._stalled if e == entity} | set(unreadable)
        open_keys |= skip or set()
        for change in plan(entity, pg, side, rows, open_keys, unmatched_notion=unmatched):
            await self._execute(change, intake)

    # ---- actions ----

    async def _execute(self, change: Change, intake: Intake) -> None:
        if self._ctx.maintenance:
            raise _Stopped
        entity, key, kind = change.entity, change.key, change.kind
        label = self._label(entity, key, change.pg, change.notion)
        if kind == "push" and key is not None:
            if change.pg is None:
                changed(self._ctx, entity, key, "delete", change.page_id)
            else:
                changed(self._ctx, entity, key)
            self._counts.pushed += 1
        elif kind in ("apply", "new_in_notion"):
            await self._intake(change, intake)
        elif kind == "conflict" and key is not None:
            await self._conflict(change, label)
        elif kind == "agree":
            if key is not None and change.page_id and change.notion is not None:
                await mirror.set_synced(self._db, entity, key, change.page_id, change.notion)
        elif kind == "deleted_in_notion":
            await self._warn(
                entity, key, texts.sync_deleted_summary(label), texts.SYNC_DELETED_TODO
            )
        elif kind == "key_changed" and key is None:
            await self._warn(
                entity, None, texts.sync_duplicate_summary(label), texts.SYNC_DUPLICATE_TODO,
                change.notion,
            )
        elif kind == "key_changed":
            await self._warn(
                entity, key, texts.sync_key_changed_summary(label, _KEY_FIELD[entity]),
                texts.SYNC_RESYNC_TODO, change.notion,
            )
        elif kind == "mass_change":
            await self._warn(
                entity, None, texts.sync_mass_summary(_PLURAL[entity]), texts.SYNC_MASS_TODO
            )

    async def _intake(self, change: Change, intake: Intake) -> None:
        # Never while holding replenish_guard: a new event takes it inside Intake.
        if await intake.apply(change) == "applied":
            self._counts.applied += 1
            return
        new = change.kind == "new_in_notion" and change.key is None
        label = self._label(change.entity, change.key, change.pg, change.notion)
        await self._warn(
            change.entity,
            change.key,
            texts.sync_rejected_summary(label, intake.last_reasons, new=new),
            texts.SYNC_RESYNC_TODO,
            change.notion,
        )

    async def _conflict(self, change: Change, label: str) -> None:
        assert change.key is not None
        summary = texts.sync_conflict_summary(label)
        opened = await issues.open_conflict(
            self._db, change.entity, change.key, summary, change.pg, change.notion
        )
        if opened is None:
            return
        self._counts.conflicts += 1
        self._stalled.add((change.entity, change.key))
        pg, notion = change.pg or {}, change.notion or {}
        fields = [*pg, *(f for f in notion if f not in pg)]
        diffs = [
            texts.sync_diff_line(
                _FIELD_LABELS.get(f) or f,
                self._value(change.entity, f, pg.get(f)),
                self._value(change.entity, f, notion.get(f)),
            )
            for f in fields
            if pg.get(f) != notion.get(f)
        ]
        await self._attention(
            texts.LOG_SYNC_CONFLICT_TITLE,
            texts.sync_conflict_text(summary, diffs),
            texts.SYNC_CONFLICT_TODO,
        )

    async def _warn(
        self,
        entity: str | None,
        key: str | None,
        summary: str,
        todo: str,
        notion: dict[str, Any] | None = None,
    ) -> None:
        """Record and post a warning, unless the same one is still open."""
        if (entity, key, summary) in self._known_warnings:
            return
        self._known_warnings.add((entity, key, summary))
        await issues.add_warning(self._db, summary, entity, key, notion)
        self._counts.warnings += 1
        await self._attention(texts.LOG_SYNC_WARNING_TITLE, summary, todo)

    async def _attention(self, title: str, what: str, todo: str) -> None:
        """Admin log entry; ``what`` holds Notion and Postgres text and is escaped here."""
        service = getattr(self._ctx, "log_service", None)
        if service is None:
            return
        try:
            await service.log_attention(title, texts.escape_text(what), todo)
        except Exception:
            logger.warning("Posting a sync entry to the log channel failed", exc_info=True)

    # ---- labels ----

    def _label(
        self, entity: str, key: str | None, pg: dict | None, notion: dict | None
    ) -> str:
        """Plain-text name of the record for summaries (escaped when posted)."""
        payload = pg or notion or {}
        if entity == "event":
            title = payload.get("event_id") or (self._titles.get(key) if key else None)
            return f'event "{title or key}"'
        if entity == "dj":
            name = payload.get("dj_name")
            if name is None and key and key.lstrip("-").isdigit():
                name = self._dj_names.get(int(key))
            return f'DJ "{name or key}"'
        if entity == "slot":
            uid, number = payload.get("event_uid"), payload.get("number")
            if uid is None and key:
                uid, _, index = key.rpartition(":")
                number = int(index) + 1 if index.isdigit() else index
            title = self._titles.get(uid) if isinstance(uid, str) else None
            return f'slot {number} of event "{title or uid}"'
        return f"{entity} {key}"

    def _value(self, entity: str, field: str, value: Any) -> str:
        if value is None:
            return "empty"
        if entity == "slot" and field == "discord_id":
            return f'"{self._dj_names.get(value, value)}"'
        if entity == "slot" and field == "event_uid":
            return f'"{self._titles.get(value, value)}"'
        return f'"{value}"'

    # ---- admin decisions ----

    async def resolve_issue(self, issue_id: int, choice: str) -> ActionResult:
        """Admin decision on an open issue.

        Conflicts: ``postgres`` (queue the bot's version) or ``notion`` (apply the
        stored Notion version through Intake; a rejection keeps the conflict open).
        Warnings: ``resynced`` (queue the entity, or everything when it has none).
        """
        if self._ctx.maintenance:
            return ActionResult(False, texts.MAINTENANCE)
        async with self._lock:
            issue = next(
                (i for i in await issues.list_open(self._db) if i.id == issue_id), None
            )
            if issue is None:
                return ActionResult(False, texts.SYNC_ISSUE_NOT_OPEN)
            if issue.kind == "conflict" and choice == "postgres":
                if await issues.resolve(self._db, issue.id, "postgres") is None:
                    return ActionResult(False, texts.SYNC_ISSUE_NOT_OPEN)
                if issue.entity and issue.entity_key:
                    changed(self._ctx, issue.entity, issue.entity_key)
                return ActionResult(True, texts.SYNC_KEPT_POSTGRES)
            if issue.kind == "conflict" and choice == "notion":
                return await self._keep_notion(issue)
            if issue.kind == "warning" and choice == "resynced":
                if await issues.resolve(self._db, issue.id, "resynced") is None:
                    return ActionResult(False, texts.SYNC_ISSUE_NOT_OPEN)
                if issue.entity and issue.entity_key:
                    changed(self._ctx, issue.entity, issue.entity_key)
                else:
                    await self.resync_all()
                return ActionResult(True, texts.SYNC_RESYNCED)
            return ActionResult(False, texts.SYNC_WRONG_CHOICE)

    async def _keep_notion(self, issue: IssueRow) -> ActionResult:
        entity, key = issue.entity, issue.entity_key
        if not entity or not key or issue.notion_payload is None:
            return ActionResult(False, texts.SYNC_WRONG_CHOICE)
        current = (await _load_pg(self._db, entity)).get(key)
        row = await mirror.get(self._db, entity, key)
        page_id = row.notion_page_id if row is not None else None
        intake = Intake(self._ctx, self._bot)
        outcome = await intake.apply(
            Change("apply", entity, key, current, issue.notion_payload, page_id)
        )
        if outcome != "applied":
            return ActionResult(False, texts.sync_notion_rejected(intake.last_reasons))
        await issues.resolve(self._db, issue.id, "notion")
        await intake.flush_log()
        return ActionResult(True, texts.SYNC_KEPT_NOTION)

    async def resync_all(self) -> int:
        """Queue every Postgres event, DJ and slot for Notion; returns the count."""
        worker = self._ctx.mirror
        if worker is None or self._db is None:
            return 0
        count = 0
        for entity in ("event", "dj", "slot"):
            for key in await _load_pg(self._db, entity):
                await outbox.enqueue(self._db, entity, key)
                count += 1
        worker.notify()
        return count
