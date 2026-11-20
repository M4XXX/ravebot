"""Restore the database from a backup file written by bot/backup/export.py.

``import_database`` is the inverse of ``export_database``: it validates the file,
writes a safety export, then replaces every exported table in one transaction.
Refusals return an ``ActionResult`` and change nothing (no safety export, no
maintenance mode).
"""
import asyncio
import base64
import contextlib
import datetime
import decimal
import gzip
import json
import logging
import uuid
from typing import Any

import asyncpg

from bot.backup.export import FORMAT, VERSION, _quote, write_safety_export
from bot.results import ActionResult
from bot.ui import texts

logger = logging.getLogger(__name__)


class _Refusal(Exception):
    """The file cannot be imported; the message is shown to the admin."""


def _decode(value: object, pg_type: str) -> object:
    """Inverse of export._encode: turn one JSON value back into an asyncpg argument."""
    if value is None:
        return None
    if pg_type.endswith("[]"):
        if not isinstance(value, list):
            raise ValueError("array value is not a list")
        return [_decode(item, pg_type[:-2]) for item in value]
    if pg_type in ("json", "jsonb"):
        return json.dumps(value)  # asyncpg's default json codec takes text
    if pg_type == "bytea":
        return base64.b64decode(str(value), validate=True)
    if pg_type in ("timestamp with time zone", "timestamp without time zone"):
        return datetime.datetime.fromisoformat(str(value))
    if pg_type == "date":
        return datetime.date.fromisoformat(str(value))
    if pg_type in ("time without time zone", "time with time zone"):
        return datetime.time.fromisoformat(str(value))
    if pg_type == "numeric":
        return decimal.Decimal(str(value))
    if pg_type == "uuid":
        return uuid.UUID(str(value))
    if pg_type == "interval":
        raise ValueError("interval columns are not supported")
    return value


def _parse(data: bytes) -> dict[str, Any]:
    try:
        document = json.loads(gzip.decompress(data).decode("utf-8"))
    except (OSError, EOFError, ValueError, UnicodeDecodeError):
        raise _Refusal(texts.IMPORT_UNREADABLE) from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise _Refusal(texts.IMPORT_NOT_A_BACKUP)
    version = document.get("version")
    if isinstance(version, bool) or version != VERSION:
        raise _Refusal(texts.IMPORT_BAD_VERSION.format(version=version))
    tables, sequences = document.get("tables"), document.get("sequences", {})
    if (
        not isinstance(tables, dict)
        or not isinstance(sequences, dict)
        or not isinstance(document.get("alembic_revision"), str)
    ):
        raise _Refusal(texts.IMPORT_NOT_A_BACKUP)
    return document


def _convert(document: dict[str, Any]) -> dict[str, tuple[list[str], list[list[object]]]]:
    """Decode every table's rows; any malformed table makes the whole file unreadable."""
    converted: dict[str, tuple[list[str], list[list[object]]]] = {}
    try:
        for name, table in document["tables"].items():
            columns = table["columns"]
            names = [c["name"] for c in columns]
            types = [c["type"] for c in columns]
            rows = []
            for row in table["rows"]:
                if len(row) != len(names):
                    raise ValueError("row length differs from the column list")
                rows.append([_decode(v, t) for v, t in zip(row, types)])
            converted[name] = (names, rows)
        for state in document["sequences"].values():
            int(state["last_value"])
            bool(state["is_called"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _Refusal(texts.IMPORT_UNREADABLE) from None
    return converted


def _prepare(data: bytes) -> tuple[dict[str, Any], dict[str, tuple[list[str], list[list[object]]]]]:
    document = _parse(data)
    return document, _convert(document)


async def _check_schema(
    conn: asyncpg.Connection,
    converted: dict[str, tuple[list[str], list[list[object]]]],
    sequences: dict[str, Any],
) -> None:
    """Refuse when the file names a table, column or sequence the database lacks."""
    actual: dict[str, set[str]] = {}
    for rec in await conn.fetch(
        "SELECT c.table_name, c.column_name FROM information_schema.columns c "
        "JOIN information_schema.tables t USING (table_schema, table_name) "
        "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
    ):
        actual.setdefault(rec["table_name"], set()).add(rec["column_name"])
    for name, (names, _rows) in converted.items():
        if name == "alembic_version" or name not in actual:
            raise _Refusal(texts.IMPORT_SCHEMA_MISMATCH.format(detail=f"table {name}"))
        missing = set(names) - actual[name]
        if missing:
            raise _Refusal(
                texts.IMPORT_SCHEMA_MISMATCH.format(detail=f"{name}: {sorted(missing)[0]}")
            )
    known = {
        r["sequencename"]
        for r in await conn.fetch("SELECT sequencename FROM pg_sequences WHERE schemaname='public'")
    }
    for seq in sequences:
        if seq not in known:
            raise _Refusal(texts.IMPORT_SCHEMA_MISMATCH.format(detail=f"sequence {seq}"))


async def _insert_order(conn: asyncpg.Connection, names: list[str]) -> list[str]:
    """Parents before children, from the foreign keys among the exported tables."""
    wanted = set(names)
    deps: dict[str, set[str]] = {n: set() for n in names}
    for rec in await conn.fetch(
        "SELECT child.relname AS child, parent.relname AS parent FROM pg_constraint k "
        "JOIN pg_class child ON child.oid = k.conrelid "
        "JOIN pg_class parent ON parent.oid = k.confrelid "
        "JOIN pg_namespace n ON n.oid = child.relnamespace "
        "WHERE k.contype = 'f' AND n.nspname = 'public'"
    ):
        if rec["child"] in wanted and rec["parent"] in wanted and rec["child"] != rec["parent"]:
            deps[rec["child"]].add(rec["parent"])
    ordered: list[str] = []
    pending = sorted(names)
    while pending:
        ready = [n for n in pending if deps[n] <= set(ordered)]
        if not ready:  # a cycle: keep the remaining order
            ordered.extend(pending)
            break
        ordered.extend(ready)
        pending = [n for n in pending if n not in ready]
    return ordered


async def _write(
    conn: asyncpg.Connection,
    converted: dict[str, tuple[list[str], list[list[object]]]],
    sequences: dict[str, Any],
) -> None:
    order = await _insert_order(conn, list(converted))
    if order:
        await conn.execute(
            "TRUNCATE " + ", ".join(f"public.{_quote(n)}" for n in order)
            + " RESTART IDENTITY CASCADE"
        )
    for name in order:
        names, rows = converted[name]
        if not rows:
            continue
        placeholders = ", ".join(f"${i}" for i in range(1, len(names) + 1))
        await conn.executemany(
            f"INSERT INTO public.{_quote(name)} ({', '.join(_quote(c) for c in names)}) "
            f"OVERRIDING SYSTEM VALUE VALUES ({placeholders})",
            rows,
        )
    for seq, state in sequences.items():
        await conn.execute(
            "SELECT setval($1::regclass, $2, $3)",
            f"public.{_quote(seq)}", int(state["last_value"]), bool(state["is_called"]),
        )


async def import_database(ctx: Any, data: bytes) -> ActionResult:
    """Replace the database content with the backup file; see the module docstring."""
    pool = ctx.db
    try:
        document, converted = await asyncio.to_thread(_prepare, data)
        running = await pool.fetchval("SELECT version_num FROM alembic_version")
        if document["alembic_revision"] != running:
            raise _Refusal(
                texts.IMPORT_REVISION_MISMATCH.format(
                    file_revision=document["alembic_revision"], running_revision=running
                )
            )
        async with pool.acquire() as conn:
            await _check_schema(conn, converted, document["sequences"])
    except _Refusal as refusal:
        return ActionResult(False, str(refusal))

    try:
        await write_safety_export(pool, "before-import")
    except Exception:
        logger.exception("The safety export before the import failed")
        return ActionResult(False, texts.IMPORT_SAFETY_FAILED)

    ctx.maintenance = texts.MAINTENANCE
    try:
        async with contextlib.AsyncExitStack() as stack:
            if ctx.sync_cycle is not None:
                # Wait for a running Notion cycle; it must not compare an old Notion
                # snapshot with the restored data.
                await stack.enter_async_context(ctx.sync_cycle.exclusive())
            try:
                async with pool.acquire() as conn, conn.transaction():
                    await _write(conn, converted, document["sequences"])
            except (asyncpg.PostgresError, asyncpg.InterfaceError):
                logger.exception("The import from the backup file failed in the database")
                return ActionResult(False, texts.IMPORT_DB_ERROR)
            if ctx.admins is not None:
                await ctx.admins.load()
            if ctx.sync_cycle is not None:
                try:
                    await ctx.sync_cycle.resync_all()
                except Exception:
                    logger.exception("Writing the restored data to Notion failed")
                    return ActionResult(True, texts.IMPORT_DONE_RESYNC_FAILED)
            return ActionResult(True, texts.IMPORT_DONE)
    finally:
        ctx.maintenance = None
