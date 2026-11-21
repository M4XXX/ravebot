"""The read-only "Bot data" mirror: bot tables other than events/slots/DJs as JSON rows.

``botdata_rows`` reads the mirrored tables from Postgres, one JSON-safe row per record
(encoding of bot/backup/export.py), keyed ``"<table>:<primary key values joined by :>"``.
Excluded: ``web_admins`` and ``log_replies`` (never mirrored), ``config`` rows of guild 0
(Notion database ids and sync state) and ``dj_conversations.logo_image`` (binary).
``restore_botdata`` writes such rows back from Notion pages (rebuild from Notion).
Hand edits in Notion are never read back during the poll: the cycle overwrites them.
"""
import json
import logging
from typing import Any

import asyncpg

from bot.backup.export import _encode, _quote
from bot.db import mirror
from bot.notion.models import (
    BOTDATA_TOO_LARGE_NOTE,
    PROP_BOTDATA_KEY,
    PROP_BOTDATA_PAYLOAD,
    PROP_BOTDATA_TABLE,
    _rich,
    _title,
)

__all__ = [
    "BOTDATA_TABLES",
    "PROP_BOTDATA_KEY",
    "PROP_BOTDATA_PAYLOAD",
    "PROP_BOTDATA_TABLE",
    "botdata_row",
    "botdata_rows",
    "is_oversized_page",
    "parse_botdata_page",
    "restore_botdata",
]

logger = logging.getLogger(__name__)

BOTDATA_TABLES = (
    "config",
    "dj_conversations",
    "outreach_sequences",
    "outreach_contacts",
    "reminders_sent",
    "fully_booked_events",
    "manual_events",
    "setup_drafts",
    "schedule_rules",
)

_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "config": ("guild_id", "key"),
    "dj_conversations": ("discord_user_id",),
    "outreach_sequences": ("event_id",),
    "outreach_contacts": ("event_id", "discord_user_id"),
    "reminders_sent": ("event_id", "discord_user_id", "slot_index"),
    "fully_booked_events": ("event_id",),
    "manual_events": ("page_id",),
    "setup_drafts": ("guild_id",),
    "schedule_rules": ("id",),
}
_EXCLUDED_COLUMNS = {"dj_conversations": {"logo_image"}}
_WHERE = {"config": " WHERE guild_id <> 0"}


async def _columns(db: asyncpg.Pool | asyncpg.Connection, table: str) -> list[tuple[str, str]]:
    rows = await db.fetch(
        "SELECT a.attname AS name, format_type(a.atttypid, NULL) AS type "
        "FROM pg_attribute a WHERE a.attrelid = to_regclass($1) "
        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
        f"public.{_quote(table)}",
    )
    skip = _EXCLUDED_COLUMNS.get(table, set())
    return [(r["name"], r["type"]) for r in rows if r["name"] not in skip]


def _key(table: str, row: dict[str, Any]) -> str:
    return f"{table}:" + ":".join(str(row[c]) for c in _PRIMARY_KEYS[table])


async def _table_rows(
    db: asyncpg.Pool | asyncpg.Connection, table: str
) -> dict[str, tuple[str, dict[str, Any]]]:
    cols = await _columns(db, table)
    if not cols:
        return {}
    select = ", ".join(_quote(c) for c, _ in cols)
    records = await db.fetch(
        f"SELECT {select} FROM public.{_quote(table)}{_WHERE.get(table, '')}"
    )
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for record in records:
        row = {c: _encode(record[c], t) for c, t in cols}
        out[_key(table, row)] = (table, row)
    return out


async def botdata_rows(pool: asyncpg.Pool) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every mirrored row: key -> (table, JSON-safe row)."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    async with pool.acquire() as conn:
        for table in BOTDATA_TABLES:
            out.update(await _table_rows(conn, table))
    return out


async def botdata_row(pool: asyncpg.Pool, key: str) -> tuple[str, dict[str, Any]] | None:
    """The current row for ``key`` (None when the record or table is gone)."""
    table = key.partition(":")[0]
    if table not in BOTDATA_TABLES:
        return None
    return (await _table_rows(pool, table)).get(key)


def parse_botdata_page(page: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None] | None:
    """(key, table, row) of a Notion page; row None when its payload is empty or invalid."""
    key, table = _title(page, PROP_BOTDATA_KEY), _select_name(page)
    if not key or not table:
        return None
    text = _rich(page, PROP_BOTDATA_PAYLOAD)
    try:
        row = json.loads(text) if text else None
    except ValueError:
        row = None
    return key, table, row if isinstance(row, dict) else None


def is_oversized_page(page: dict[str, Any]) -> bool:
    """True for a page whose payload was left out because it exceeds Notion's limit."""
    return not _rich(page, PROP_BOTDATA_PAYLOAD) and _rich(page, "Note") == BOTDATA_TOO_LARGE_NOTE


def _select_name(page: dict[str, Any]) -> str | None:
    sel = ((page.get("properties") or {}).get(PROP_BOTDATA_TABLE) or {}).get("select")
    return sel.get("name") if sel else None


async def restore_botdata(conn: asyncpg.Connection, pages: list[dict[str, Any]]) -> int:
    """Write the rows of Bot data pages back into their tables; returns how many.

    Unknown tables, unreadable payloads and columns the table no longer has are skipped.
    An existing row with the same primary key is overwritten. Each restored page is recorded
    in the mirror as synced.
    """
    restored = 0
    parsed_pages = [(page, parsed) for page in pages if (parsed := parse_botdata_page(page))]
    # Parents before children (outreach_contacts has a foreign key to outreach_sequences);
    # Notion's page order is arbitrary.
    parsed_pages.sort(
        key=lambda item: BOTDATA_TABLES.index(item[1][1])
        if item[1][1] in BOTDATA_TABLES
        else len(BOTDATA_TABLES)
    )
    for page, (key, table, row) in parsed_pages:
        if table not in BOTDATA_TABLES or row is None:
            logger.warning("Skipping Bot data page %r", key)
            continue
        known = {c for c, _ in await _columns(conn, table)}
        pk = _PRIMARY_KEYS[table]
        data = {c: v for c, v in row.items() if c in known}
        if not all(c in data for c in pk):
            logger.warning("Skipping Bot data page %r: primary key missing", key)
            continue
        if table == "config" and data["guild_id"] == 0:
            continue
        cols = ", ".join(_quote(c) for c in data)
        updates = [c for c in data if c not in pk]
        action = (
            "DO UPDATE SET " + ", ".join(f"{_quote(c)} = EXCLUDED.{_quote(c)}" for c in updates)
            if updates
            else "DO NOTHING"
        )
        await conn.execute(
            f"INSERT INTO public.{_quote(table)} ({cols}) "
            f"SELECT {cols} FROM jsonb_populate_record(null::public.{_quote(table)}, $1::jsonb) "
            f"ON CONFLICT ({', '.join(_quote(c) for c in pk)}) {action}",
            json.dumps(data),
        )
        # The page already holds this row: record it so the next cycle leaves it alone.
        await mirror.set_synced(conn, "botdata", key, str(page["id"]), row)
        restored += 1
    if any(parsed[1] == "schedule_rules" for _, parsed in parsed_pages):
        # Rows came back with explicit ids: move the serial past them.
        await conn.execute(
            "SELECT setval(pg_get_serial_sequence('schedule_rules', 'id'),"
            " COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM schedule_rules"
        )
    return restored
