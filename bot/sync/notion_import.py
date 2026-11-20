"""Import events, slots and DJs from Notion into Postgres in one transaction.

``upgrade`` runs once on the first start after the switch to Postgres as the source
of truth (``run_upgrade_import_if_needed``): events keep their Notion page id as
``uid`` (or their ``Bot ID`` when the bot wrote one), slot holders come from Notion,
DJs missing in ``djs`` are added (existing rows keep their profile), and every
imported page gets a ``notion_mirror`` row with its Notion payload as synced state.
``replace`` (rebuild from Notion) empties the tables first, then does the same.

Unreadable pages are skipped and listed in ``ImportResult.notes``. Integrity
problems (duplicate titles or Discord IDs, a booked slot whose event is unknown or
whose number is outside 1..slot_count, a DJ name already taken) roll everything back
and are listed in ``ImportResult.problems``.
"""
import dataclasses
import datetime
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import asyncpg

from bot import maintenance
from bot.backup.export import write_safety_export
from bot.db import mirror, outbox
from bot.db.config import get_value, set_value
from bot.db.events import EventDraft, insert_event
from bot.notion.client import PROP_DJ_LOGO
from bot.notion.models import (
    PROP_EVENT_BOT_ID,
    PROP_EVENT_ID,
    PROP_SLOT_DJ,
    PROP_SLOT_EVENT,
    PROP_SLOT_NUMBER,
    _number,
    _relation_ids,
    _rich,
    _title,
    parse_dj,
)
from bot.sync.botdata import restore_botdata
from bot.sync.payloads import (
    PROP_DJ_ALT_LINK,
    PROP_DJ_LINK,
    PROP_DJ_VRCHAT_NAME,
    _url,
    dj_payload,
    notion_dj_payload,
    notion_event_payload,
    notion_slot_payload,
    payload_hash,
)
from bot.ui import texts

__all__ = [
    "PG_TRUTH_IMPORTED_KEY",
    "pause_if_import_pending",
    "ImportResult",
    "import_from_notion",
    "post_upgrade_result",
    "run_upgrade_import_if_needed",
]

logger = logging.getLogger(__name__)

# Global config key (guild 0, like the Notion database ids): "1" once the upgrade import ran.
PG_TRUTH_IMPORTED_KEY = "pg_truth_imported"
CONFIG_GUILD_ID = 0

_REPLACE_TABLES = "events, slot_locks, djs, notion_mirror, notion_outbox, sync_issues"


@dataclass
class ImportResult:
    ok: bool
    events: int
    slots: int
    djs: int
    problems: list[str]
    notes: list[str] = field(default_factory=list)
    # replace mode only: the first Notion ``Logo`` file of each imported DJ by Discord ID,
    # as (kind, url) with kind "file" (Notion-hosted, signed URL) or "external".
    logos: dict[int, tuple[str, str]] = field(default_factory=dict)


@dataclass
class _Event:
    page_id: str
    uid: str
    draft: EventDraft
    payload: dict[str, Any]
    slot_pages: dict[int, dict[str, Any]] = field(default_factory=dict)  # 0-based index


@dataclass
class _Dj:
    page_id: str
    discord_id: int | None
    name: str
    payload: dict[str, Any]  # what the Notion page holds (discord_id None when missing)
    logo: tuple[str, str] | None = None  # first Logo file: (kind, url)


class _Rollback(Exception):
    """Integrity problems found inside the transaction: undo everything."""


def _norm(name: str) -> str:
    """DJ name key as the bot compares names (see djs.dj_name_exists)."""
    return " ".join(name.split()).lower()


def _event_label(page: dict[str, Any]) -> str:
    title = _title(page, PROP_EVENT_ID)
    return f"'{title}'" if title else f"page {page.get('id', '?')}"


def _draft(payload: dict[str, Any]) -> EventDraft:
    hour, minute = payload["start"].split(":")
    return EventDraft(
        event_id=payload["event_id"],
        date=datetime.date.fromisoformat(payload["date"]),
        start=datetime.time(int(hour), int(minute)),
        slot_minutes=payload["slot_minutes"],
        slot_count=payload["slot_count"],
        status=payload["status"],
        genre=payload["genre"],
        notes=payload["notes"],
    )


def _plan_events(
    pages: list[dict[str, Any]], problems: list[str], notes: list[str]
) -> list[_Event]:
    events: list[_Event] = []
    titles: set[str] = set()
    uids: set[str] = set()
    for page in pages:
        payload = notion_event_payload(page)
        if payload is None:
            notes.append(f"Event {_event_label(page)} skipped: a required property is empty")
            continue
        uid = _rich(page, PROP_EVENT_BOT_ID) or page["id"]
        title = payload["event_id"]
        if title in titles:
            problems.append(f"Two Notion events have the title '{title}'")
            continue
        if uid in uids:
            problems.append(f"Two Notion events have the Bot ID '{uid}'")
            continue
        titles.add(title)
        uids.add(uid)
        events.append(_Event(page["id"], uid, _draft(payload), payload))
    return events


def _plan_djs(pages: list[dict[str, Any]], problems: list[str], notes: list[str]) -> list[_Dj]:
    result: list[_Dj] = []
    ids: set[int] = set()
    names: set[str] = set()
    for page in pages:
        dj = parse_dj(page)
        if not dj.name:
            notes.append(f"DJ page {dj.page_id} skipped: it has no DJ name")
            continue
        payload = notion_dj_payload(page)
        if payload is None:  # a name but no (valid) Discord ID
            payload = {
                "discord_id": None,
                "dj_name": dj.name,
                "vrchat_name": _rich(page, PROP_DJ_VRCHAT_NAME),
                "link": _url(page, PROP_DJ_LINK),
                "alt_link": _url(page, PROP_DJ_ALT_LINK),
            }
        if dj.discord_id is not None:
            if dj.discord_id in ids:
                problems.append(f"Two Notion DJs have the Discord ID {dj.discord_id}")
                continue
            ids.add(dj.discord_id)
        if _norm(dj.name) in names:
            problems.append(f"Two Notion DJs have the name '{dj.name}'")
            continue
        names.add(_norm(dj.name))
        result.append(_Dj(dj.page_id, dj.discord_id, dj.name, payload, _logo_file(page)))
    return result


def _logo_file(page: dict[str, Any]) -> tuple[str, str] | None:
    """The first file of the DJ page's ``Logo`` property as (kind, url), or None."""
    prop = (page.get("properties") or {}).get(PROP_DJ_LOGO) or {}
    for item in prop.get("files") or []:
        kind = item.get("type")
        if kind in ("file", "external"):
            url = (item.get(kind) or {}).get("url")
            if isinstance(url, str) and url.strip():
                return kind, url.strip()
    return None


def _plan_slots(
    pages: list[dict[str, Any]],
    events: list[_Event],
    dj_pages: set[str],
    problems: list[str],
    notes: list[str],
) -> None:
    """Attach each readable slot page to its event; booked orphans are problems."""
    by_page = {e.page_id: e for e in events}
    for page in pages:
        event_ids = _relation_ids(page, PROP_SLOT_EVENT)
        djs = _relation_ids(page, PROP_SLOT_DJ)
        number = _number(page, PROP_SLOT_NUMBER)
        found = (problems if djs else notes).append
        event = by_page.get(event_ids[0]) if event_ids else None
        if event is None:
            found(f"Slot page {page['id']} has no readable event")
            continue
        title = event.draft.event_id
        count = event.draft.slot_count
        if number is None or number != int(number) or not 1 <= number <= count:
            found(f"Slot {number} of '{title}' is outside 1..{count}")
            continue
        index = int(number) - 1
        slot = f"Slot {index + 1} of '{title}'"
        taken = event.slot_pages.get(index)
        if taken is not None:
            if _relation_ids(taken, PROP_SLOT_DJ) or not djs:
                notes.append(f"{slot} has a second Notion row; skipped")
                continue
            notes.append(f"{slot} has a second Notion row; the empty one is skipped")
        if djs and djs[0] not in dj_pages:
            notes.append(f"{slot}: its DJ page is unreadable, imported as free")
        if len(djs) > 1:
            notes.append(f"{slot} has more than one DJ; the first is kept")
        event.slot_pages[index] = page


async def import_from_notion(
    pool: asyncpg.Pool, notion_client: Any, *, mode: Literal["upgrade", "replace"]
) -> ImportResult:
    """Read every Notion page and write events, slots and DJs in one transaction."""
    if mode not in ("upgrade", "replace"):
        raise ValueError(f"unknown import mode: {mode}")
    try:
        snapshot = await notion_client.read_all()
    except Exception as exc:
        logger.warning("Reading Notion for the import failed", exc_info=True)
        return ImportResult(False, 0, 0, 0, [f"Notion could not be read: {exc}"])

    problems: list[str] = []
    notes: list[str] = []
    events = _plan_events(snapshot.events, problems, notes)
    djs = _plan_djs(snapshot.djs, problems, notes)
    _plan_slots(snapshot.slots, events, {d.page_id for d in djs}, problems, notes)
    if problems:
        return ImportResult(False, 0, 0, 0, problems, notes)

    try:
        async with pool.acquire() as conn, conn.transaction():
            counts = await _write(
                conn, mode, events, djs, problems, notes, snapshot.botdata
            )
    except _Rollback:
        return ImportResult(False, 0, 0, 0, problems, notes)
    except asyncpg.PostgresError as exc:
        logger.exception("The Notion import failed in the database")
        return ImportResult(False, 0, 0, 0, [f"Database error: {exc}"], notes)
    logos = (
        {d.discord_id: d.logo for d in djs if d.logo and d.discord_id is not None}
        if mode == "replace"
        else {}
    )
    return ImportResult(True, *counts, problems=[], notes=notes, logos=logos)


async def _write(
    conn: asyncpg.Connection,
    mode: str,
    events: list[_Event],
    djs: list[_Dj],
    problems: list[str],
    notes: list[str],
    botdata: list[dict[str, Any]],
) -> tuple[int, int, int]:
    if mode == "replace":
        await conn.execute(f"TRUNCATE {_REPLACE_TABLES}")
    new_events = await _check_events(conn, events, problems, notes)
    new_djs = await _check_djs(conn, djs, problems, notes)
    if problems:
        raise _Rollback
    await _assign_guest_ids(conn, djs)

    for dj in new_djs:
        p = dj.payload
        await conn.execute(
            """
            INSERT INTO djs (
                discord_user_id, dj_name, vrchat_name, stream_url_primary,
                stream_url_backup, logo_synced, active_booking, onboarding_complete,
                onboarding_step
            )
            VALUES ($1, $2, $3, $4, $5, true, false, true, 0)
            """,
            dj.discord_id, dj.name, p["vrchat_name"], p["link"], p["alt_link"],
        )
    await _mirror_djs(conn, djs)

    uid_by_page = {e.page_id: e.uid for e in new_events}
    id_by_dj_page: dict[str, int | None] = {d.page_id: d.discord_id for d in djs}
    slots = 0
    for event in new_events:
        await insert_event(conn, event.draft, uid=event.uid)
        await mirror.set_synced(conn, "event", event.uid, event.page_id, event.payload)
        holders: dict[int, int | None] = {}
        for index, page in event.slot_pages.items():
            payload = notion_slot_payload(page, uid_by_page, id_by_dj_page)
            if payload is None:  # cannot happen after the checks above
                continue
            holders[index] = payload["discord_id"]
            await mirror.set_synced(conn, "slot", f"{event.uid}:{index}", page["id"], payload)
            slots += 1
        await _set_holders(conn, event.uid, event.draft.slot_count, holders)
        for index in range(event.draft.slot_count):
            if index not in event.slot_pages:  # no Notion row yet: the worker creates it
                await outbox.enqueue(conn, "slot", f"{event.uid}:{index}")
    if mode == "replace":
        # Upgrade keeps what Postgres already holds; a rebuild restores the bot tables.
        await restore_botdata(conn, botdata)
    return len(new_events), slots, len(djs)


async def _check_events(
    conn: asyncpg.Connection, events: list[_Event], problems: list[str], notes: list[str]
) -> list[_Event]:
    """Events to insert; an event already in Postgres under its uid is kept as it is."""
    rows = await conn.fetch("SELECT uid, event_id FROM events")
    uids = {r["uid"] for r in rows}
    titles = {r["event_id"] for r in rows}
    result = []
    for event in events:
        title = event.draft.event_id
        if event.uid in uids:
            notes.append(f"Event '{title}' is already in the database; kept as it is")
        elif title in titles:
            problems.append(f"An event titled '{title}' already exists in the database")
        else:
            result.append(event)
    return result


async def _check_djs(
    conn: asyncpg.Connection, djs: list[_Dj], problems: list[str], notes: list[str]
) -> list[_Dj]:
    """DJs to insert. Existing rows win; a page without Discord ID joins a same-named DJ."""
    rows = await conn.fetch("SELECT discord_user_id, dj_name FROM djs")
    ids = {r["discord_user_id"] for r in rows}
    by_name = {_norm(r["dj_name"]): r["discord_user_id"] for r in rows}
    claimed = {d.discord_id for d in djs if d.discord_id is not None}
    result = []
    for dj in djs:
        owner = by_name.get(_norm(dj.name))
        if dj.discord_id is None:
            if owner is not None and owner not in claimed:
                dj.discord_id = owner
                claimed.add(owner)
                notes.append(f"DJ '{dj.name}' has no Discord ID in Notion; matched by name")
                continue
            if owner is not None:
                problems.append(f"The DJ name '{dj.name}' is already used by another DJ")
                continue
            notes.append(f"DJ '{dj.name}' has no Discord ID in Notion; added as a guest")
            result.append(dj)  # gets a guest id later
        elif dj.discord_id not in ids:
            if owner is not None:
                problems.append(f"The DJ name '{dj.name}' is already used by another DJ")
            else:
                result.append(dj)
    return result


async def _assign_guest_ids(conn: asyncpg.Connection, djs: list[_Dj]) -> None:
    """Keep the guest id sequence below every imported guest id, then hand out new ones."""
    lowest = min((d.discord_id for d in djs if d.discord_id is not None), default=0)
    if lowest < 0:
        row = await conn.fetchrow("SELECT last_value, is_called FROM guest_dj_id_seq")
        upcoming = row["last_value"] - 1 if row["is_called"] else row["last_value"]
        if upcoming >= lowest:
            await conn.execute("SELECT setval('guest_dj_id_seq', $1, true)", lowest)
    for dj in djs:
        if dj.discord_id is None:
            dj.discord_id = await conn.fetchval("SELECT nextval('guest_dj_id_seq')")


async def _mirror_djs(conn: asyncpg.Connection, djs: list[_Dj]) -> None:
    """Notion payload as synced state; queue the DJs whose Postgres row differs."""
    rows = await conn.fetch(
        "SELECT discord_user_id, dj_name, vrchat_name, stream_url_primary, stream_url_backup"
        " FROM djs WHERE discord_user_id = ANY($1::bigint[])",
        [d.discord_id for d in djs],
    )
    current = {r["discord_user_id"]: payload_hash(dj_payload(r)) for r in rows}
    for dj in djs:
        key = str(dj.discord_id)
        await mirror.set_synced(conn, "dj", key, dj.page_id, dj.payload)
        if current.get(dj.discord_id) != payload_hash(dj.payload):
            await outbox.enqueue(conn, "dj", key)


async def _set_holders(
    conn: asyncpg.Connection, uid: str, slot_count: int, holders: dict[int, int | None]
) -> None:
    """Notion's holder for every slot 0..slot_count-1 (free when Notion has none).

    Rows left from before the upgrade (keyed by the same page id) keep their
    booked_at when the holder is unchanged.
    """
    await conn.executemany(
        """
        UPDATE slot_locks SET
            booked_at = CASE
                WHEN $3::bigint IS NULL THEN NULL
                WHEN discord_user_id IS NOT DISTINCT FROM $3::bigint
                    THEN COALESCE(booked_at, now())
                ELSE now()
            END,
            discord_user_id = $3::bigint
        WHERE event_id = $1 AND slot_index = $2
        """,
        [(uid, index, holders.get(index)) for index in range(slot_count)],
    )


# --- upgrade gate ---


async def run_upgrade_import_if_needed(ctx: Any) -> None:
    """First start after the upgrade: safety export, then the import from Notion.

    Does nothing without Notion, once the import ran (config key, guild 0) or while
    another import or rebuild holds the claim (bot/maintenance.py, R20). While it
    runs, and after a failure, ``ctx.maintenance`` is set: bookings and edits are refused
    and the Notion mirror stays off. A failed import is retried on the next start.
    The result waits in ``ctx.upgrade_import`` for ``post_upgrade_result``.
    """
    db = ctx.db
    if ctx.notion_client is None or db is None:
        return
    if await get_value(db, CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY) == "1":
        return
    claim = maintenance.try_claim("upgrade")
    if claim is None:  # a backup import or rebuild runs (admin page): the next start
        logger.warning("Another import or rebuild is running; the upgrade import waits")
        return
    with claim:
        ctx.maintenance = texts.MAINTENANCE
        result = await _export_and_import(ctx)
        if result.ok:
            try:
                await set_value(db, CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY, "1")
            except Exception as exc:
                logger.exception("Recording the finished Notion import failed")
                result = dataclasses.replace(
                    result, ok=False, problems=[f"Recording the finished import failed: {exc}"]
                )
        ctx.upgrade_import = result
        if result.ok:
            ctx.maintenance = None
            ctx.sync_problem = None
            logger.info(
                "Imported %d events, %d slots and %d DJs from Notion",
                result.events, result.slots, result.djs,
            )
        else:
            ctx.sync_problem = "; ".join(result.problems[:10])
            logger.error("The import from Notion failed: %s", ctx.sync_problem)


async def pause_if_import_pending(ctx: Any) -> None:
    """Decision 8: Notion is set up but its databases could not be read at startup.

    While the one-time upgrade import has not run, the bot must not work on an empty
    or partial database (replenish would create events, outreach would DM DJs): pause
    like a failed import (``ctx.maintenance``, ``ctx.sync_problem``). The pause ends
    when the import succeeds (admin page: retry the import or create new databases,
    or the next start). Only called when Notion is configured (decision 10).
    """
    db = ctx.db
    if ctx.notion_client is not None or db is None:
        return
    try:
        if await get_value(db, CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY) == "1":
            return
    except Exception:
        logger.exception("Reading the upgrade import mark failed; pausing to be safe")
    ctx.maintenance = texts.MAINTENANCE
    ctx.sync_problem = texts.UPGRADE_WAITS_FOR_NOTION
    logger.error("Notion databases missing before the one-time import: the bot is paused")


async def _export_and_import(ctx: Any) -> ImportResult:
    try:
        path = await write_safety_export(ctx.db, "upgrade")
    except Exception as exc:
        logger.exception("The safety export before the Notion import failed")
        return ImportResult(
            False, 0, 0, 0, [f"The safety export failed, nothing was imported: {exc}"]
        )
    logger.info("Safety export written to %s", path)
    try:
        return await import_from_notion(ctx.db, ctx.notion_client, mode="upgrade")
    except Exception as exc:
        logger.exception("The import from Notion failed")
        return ImportResult(False, 0, 0, 0, [f"The import failed: {exc}"])


async def post_upgrade_result(ctx: Any) -> None:
    """Post a waiting upgrade import result to the admin log once. Never raises."""
    result, ctx.upgrade_import = ctx.upgrade_import, None
    log_service = ctx.log_service
    if result is None or log_service is None:
        return
    try:
        if result.ok:
            notes = [texts.escape_text(n) for n in result.notes]
            await log_service.log_notice(
                texts.LOG_UPGRADE_DONE_TITLE,
                texts.log_upgrade_done_text(result.events, result.slots, result.djs, notes),
            )
        else:
            problems = [texts.escape_text(p) for p in result.problems]
            await log_service.log_attention(
                texts.LOG_UPGRADE_FAILED_TITLE,
                texts.log_upgrade_failed_text(problems),
                texts.LOG_UPGRADE_FAILED_TODO,
            )
    except Exception:
        logger.warning("Could not post the Notion import result", exc_info=True)
