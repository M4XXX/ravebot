"""Full database export to a gzip-compressed JSON file.

File layout (format "ravebot-backup", version 1)::

    {
      "format": "ravebot-backup", "version": 1,
      "alembic_revision": "<alembic_version.version_num>",
      "created_at": "<UTC ISO timestamp>",
      "tables": {
        "<table>": {
          "columns": [{"name": "<column>", "type": "<pg type>"}, ...],
          "rows": [[<value per column, in column order>], ...]
        }
      },
      "sequences": {"<sequence>": {"last_value": <int>, "is_called": <bool>}}
    }

Every base table in schema public except alembic_version is exported, as are
all sequences in schema public. Each column's "type" is the Postgres type name
from ``format_type`` (e.g. "bytea", "timestamp with time zone", "date", "jsonb",
"bigint", "text[]"), so a restorer can decode values without guessing.

Value encoding: bytea as a base64 string; timestamps, dates and times as ISO 8601
strings; json/jsonb as the JSON value itself; numeric, uuid and interval as
strings; everything else as JSON scalars. NULL is null. Arrays are JSON lists
with each element encoded by the same rules.

Restore: after inserting rows, call ``setval(seq, last_value, is_called)``.
"""
import asyncio
import base64
import datetime
import decimal
import gzip
import json
import logging
import os
import re
import uuid
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

FORMAT = "ravebot-backup"
VERSION = 1
KEEP_SAFETY_FILES = 10
DEFAULT_BACKUP_DIR = "/data/backups"
FALLBACK_BACKUP_DIR = "./backups"
_SUFFIX = ".ravebot.json.gz"
_FILE_RE = re.compile(r"^ravebot-\d{8}-\d{6}-.+" + re.escape(_SUFFIX) + r"$")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _encode(value: object, pg_type: str) -> object:
    """Encode one column value into a JSON-safe value (see module docstring)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        element_type = pg_type[:-2] if pg_type.endswith("[]") else pg_type
        return [_encode(item, element_type) for item in value]
    if pg_type in ("json", "jsonb"):
        return json.loads(value) if isinstance(value, str) else value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (decimal.Decimal, uuid.UUID, datetime.timedelta)):
        return str(value)
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


async def export_database(pool: asyncpg.Pool) -> bytes:
    """Return the gzip-compressed JSON export of every table in schema public."""
    tables: dict[str, dict] = {}
    sequences: dict[str, dict] = {}
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            revision = await conn.fetchval("SELECT version_num FROM alembic_version")
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            names = [
                r["table_name"]
                for r in await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "AND table_name <> 'alembic_version' ORDER BY table_name"
                )
            ]
            for name in names:
                cols = await conn.fetch(
                    "SELECT a.attname AS name, format_type(a.atttypid, NULL) AS type "
                    "FROM pg_attribute a WHERE a.attrelid = to_regclass($1) "
                    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
                    f"public.{_quote(name)}",
                )
                types = [c["type"] for c in cols]
                rows = await conn.fetch(f"SELECT * FROM public.{_quote(name)}")
                tables[name] = {
                    "columns": [{"name": c["name"], "type": c["type"]} for c in cols],
                    "rows": [
                        [_encode(row[c["name"]], t) for c, t in zip(cols, types)] for row in rows
                    ],
                }
            for rec in await conn.fetch(
                "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' "
                "ORDER BY sequencename"
            ):
                seq = rec["sequencename"]
                state = await conn.fetchrow(
                    f"SELECT last_value, is_called FROM public.{_quote(seq)}"
                )
                sequences[seq] = {
                    "last_value": state["last_value"], "is_called": state["is_called"]
                }
    document = {
        "format": FORMAT,
        "version": VERSION,
        "alembic_revision": revision,
        "created_at": created_at,
        "tables": tables,
        "sequences": sequences,
    }
    return gzip.compress(json.dumps(document, ensure_ascii=False).encode("utf-8"))


def _make_private_dir(path: Path) -> None:
    """Create the directory with mode 0o700; an existing directory keeps its mode."""
    created = not path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created:
        path.chmod(0o700)  # mkdir's mode is masked by the umask


def _backup_dir() -> Path:
    """BACKUP_DIR (default /data/backups), or ./backups when it is not writable."""
    preferred = Path(os.environ.get("BACKUP_DIR") or DEFAULT_BACKUP_DIR)
    try:
        _make_private_dir(preferred)
        if os.access(preferred, os.W_OK):
            return preferred
    except OSError:
        pass
    fallback = Path(FALLBACK_BACKUP_DIR)
    _make_private_dir(fallback)
    log.warning("Backup directory %s is not writable, using %s", preferred, fallback)
    return fallback


def _write_file(data: bytes, reason: str) -> Path:
    directory = _backup_dir()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_reason = re.sub(r"[^A-Za-z0-9_-]+", "-", reason).strip("-") or "backup"
    path = directory / f"ravebot-{stamp}-{safe_reason}{_SUFFIX}"
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    tmp.chmod(0o600)  # a leftover tmp file keeps its old mode through O_TRUNC
    tmp.replace(path)
    files = sorted(p for p in directory.iterdir() if _FILE_RE.match(p.name))
    for old in files[:-KEEP_SAFETY_FILES]:
        old.unlink(missing_ok=True)
    return path


async def write_safety_export(pool: asyncpg.Pool, reason: str) -> Path:
    """Write a full export to the backup directory and keep the newest 10 files."""
    data = await export_database(pool)
    return await asyncio.to_thread(_write_file, data, reason)
