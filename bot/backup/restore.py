"""Restore the database from a backup file written by bot/backup/export.py.

``import_database`` is the inverse of ``export_database``: it validates the file,
writes a safety export, then replaces every exported table in one transaction.
Refusals return an ``ActionResult`` and change nothing (no safety export, no
maintenance mode); another import or rebuild running is one of them.

A successful import is a full recovery, like a rebuild (R24): with Notion it marks the
upgrade import done (in the same transaction, after the restored config), it clears
``ctx.maintenance`` and ``ctx.sync_problem``, starts the Notion mirror if it was not
running and queues everything for Notion. A failed import keeps the earlier pause.
With Notion it also sets the post-restore flag: the next sync cycle moves Notion pages
the restored mirror does not know (made after the backup) to the Notion trash with a
warning instead of applying them.

The file is unpacked in chunks and refused above ``MAX_UNPACKED`` bytes.
"""
import asyncio
import base64
import contextlib
import datetime
import decimal
import gzip
import io
import json
import logging
import uuid
from typing import Any

import asyncpg

from bot import maintenance
from bot.backup.export import FORMAT, VERSION, _quote, write_safety_export
from bot.db import pool as db_pool
from bot.db.config import set_value
from bot.results import ActionResult
from bot.sync.mirror_start import ensure_mirror
from bot.sync.notion_import import CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY, POST_RESTORE_KEY
from bot.ui import texts

logger = logging.getLogger(__name__)

# Most bytes a backup file may unpack to (a gzip bomb is refused, M1). An export of the
# whole database stays far below this; uploads are limited to 512 MB packed.
MAX_UNPACKED = 1024 * 1024 * 1024
_CHUNK = 1024 * 1024


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


def _unpack(data: bytes) -> bytearray:
    """Decompress in chunks; refuse once the output passes ``MAX_UNPACKED``.

    Returns the buffer itself (no ``bytes`` copy); ``_parse`` decodes it and drops it
    before the JSON is parsed, so at most two unpacked copies exist at once.
    """
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        while chunk := stream.read(_CHUNK):
            out += chunk
            if len(out) > MAX_UNPACKED:
                raise _Refusal(texts.IMPORT_TOO_LARGE)
    return out


def _parse(data: bytes) -> dict[str, Any]:
    try:
        raw = _unpack(data)
        text = raw.decode("utf-8")
        del raw  # free the unpacked bytes before the parsed document is built
        document = json.loads(text)
        del text
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
            if not isinstance(state["is_called"], bool):
                raise ValueError("is_called is not a boolean")
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


def _is_older(file_revision: str, running: str) -> bool:
    """True when the file's revision is one of ours and comes before the running one."""
    order = db_pool.revision_order()
    return file_revision in order and running in order and (
        order.index(file_revision) < order.index(running)
    )


async def _load_old_revision(
    ctx: Any,
    converted: dict[str, tuple[list[str], list[list[object]]]],
    document: dict[str, Any],
    running: str,
    safety_file: Any,
) -> ActionResult | None:
    """Bring a backup of an older version into the database; None when it worked.

    The schema goes down to the file's revision (one transaction), the rows are written,
    then the schema goes up to the current revision again (one transaction), which also
    migrates the rows. Everything in the database is replaced anyway, and the safety
    export holds the current data. A failure before the new data is in is undone by
    going up again and writing the safety export back.
    """
    pool = ctx.db
    url = ctx.config.postgres_url
    ok, detail = await db_pool.run_alembic(url, "downgrade", document["alembic_revision"])
    if not ok:
        logger.error("The import could not go down to the backup's revision: %s", detail)
        return ActionResult(False, texts.IMPORT_MIGRATE_FAILED)
    try:
        async with pool.acquire() as conn, conn.transaction():
            await _check_schema(conn, converted, document["sequences"])
            await _write(conn, converted, document["sequences"])
    except (_Refusal, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        logger.error("The old backup does not fit its own revision: %s", exc)
        return await _undo_migration(ctx, safety_file)
    ok, detail = await db_pool.run_alembic(url, "upgrade", "head")
    if not ok:
        logger.error("Migrating the imported backup failed: %s", detail)
        # The rows are in, at the old revision. Try to go back to the current data.
        return await _undo_migration(ctx, safety_file)
    return None


async def _undo_migration(ctx: Any, safety_file: Any) -> ActionResult:
    """Go up to the current revision and write the safety export back."""
    pool = ctx.db
    ok, _detail = await db_pool.run_alembic(ctx.config.postgres_url, "upgrade", "head")
    if ok:
        try:
            data = await asyncio.to_thread(safety_file.read_bytes)
            document, converted = await asyncio.to_thread(_prepare, data)
            async with pool.acquire() as conn, conn.transaction():
                await _check_schema(conn, converted, document["sequences"])
                await _write(conn, converted, document["sequences"])
            return ActionResult(False, texts.IMPORT_MIGRATE_FAILED)
        except Exception:
            logger.exception("Writing the safety export back failed")
    return ActionResult(False, texts.IMPORT_MIGRATE_STUCK)


async def import_database(ctx: Any, data: bytes) -> ActionResult:
    """Replace the database content with the backup file; see the module docstring.

    Refused at once while another import or rebuild runs (bot/maintenance.py, R20).
    """
    claim = maintenance.try_claim("import")
    if claim is None:
        return ActionResult(False, texts.OPERATION_BUSY)
    with claim:
        return await _import_database(ctx, data)


async def _import_database(ctx: Any, data: bytes) -> ActionResult:
    pool = ctx.db
    try:
        document, converted = await asyncio.to_thread(_prepare, data)
        running = await pool.fetchval("SELECT version_num FROM alembic_version")
        file_revision = document["alembic_revision"]
        migrating = file_revision != running
        if migrating and not _is_older(file_revision, running):
            raise _Refusal(
                texts.IMPORT_REVISION_MISMATCH.format(
                    file_revision=file_revision, running_revision=running
                )
            )
        if not migrating:
            async with pool.acquire() as conn:
                await _check_schema(conn, converted, document["sequences"])
    except _Refusal as refusal:
        return ActionResult(False, str(refusal))

    try:
        safety_file = await write_safety_export(pool, "before-import")
    except Exception:
        logger.exception("The safety export before the import failed")
        return ActionResult(False, texts.IMPORT_SAFETY_FAILED)

    previous = (ctx.maintenance, getattr(ctx, "sync_problem", None))
    notion = getattr(ctx, "notion_client", None) is not None
    written = False
    keep_pause = False  # a failed migration leaves the old schema: stay paused
    ctx.maintenance = texts.MAINTENANCE
    try:
        async with contextlib.AsyncExitStack() as stack:
            if ctx.sync_cycle is not None:
                # Wait for a running Notion cycle; it must not compare an old Notion
                # snapshot with the restored data.
                await stack.enter_async_context(ctx.sync_cycle.exclusive())
            if migrating:
                stopped = await _load_old_revision(
                    ctx, converted, document, running, safety_file
                )
                if stopped is not None:
                    keep_pause = stopped.message == texts.IMPORT_MIGRATE_STUCK
                    return stopped
            try:
                async with pool.acquire() as conn, conn.transaction():
                    if not migrating:
                        await _write(conn, converted, document["sequences"])
                    if notion:
                        # After the restored config, so the file cannot drop the mark.
                        await set_value(conn, CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY, "1")
                        # The next sync cycle trashes Notion pages newer than the backup
                        # instead of taking them in (I4); survives a restart.
                        await set_value(conn, CONFIG_GUILD_ID, POST_RESTORE_KEY, "1")
            except (asyncpg.PostgresError, asyncpg.InterfaceError):
                logger.exception("The import from the backup file failed in the database")
                return ActionResult(False, texts.IMPORT_DB_ERROR)
            written = True
            if ctx.admins is not None:
                await ctx.admins.load()
            done_text = texts.IMPORT_DONE_MIGRATED if migrating else texts.IMPORT_DONE
            result = ActionResult(True, done_text)
            if ctx.sync_cycle is not None:
                try:
                    await ctx.sync_cycle.resync_all()
                except Exception:
                    logger.exception("Writing the restored data to Notion failed")
                    result = ActionResult(True, texts.IMPORT_DONE_RESYNC_FAILED)
    finally:
        if written:
            ctx.maintenance = None
            ctx.sync_problem = None
        elif keep_pause:
            pass
        else:
            ctx.maintenance, ctx.sync_problem = previous
    # A bot paused by a failed upgrade import has no mirror yet: start it and queue all.
    await ensure_mirror(ctx, "the backup import")
    return result
