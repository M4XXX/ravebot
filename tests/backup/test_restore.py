"""Unit checks of the backup import (no database): decoding and refusals."""
import base64
import datetime
import decimal
import gzip
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.backup import export, restore
from bot.ui import texts


def _file(**override: object) -> bytes:
    doc: dict = {
        "format": "ravebot-backup", "version": 1, "alembic_revision": "0017",
        "created_at": "2026-01-01T00:00:00+00:00", "tables": {}, "sequences": {},
    }
    doc.update(override)
    return gzip.compress(json.dumps(doc).encode())


def _ctx(revision: str = "0017") -> MagicMock:
    ctx = MagicMock()
    ctx.maintenance = None
    ctx.db.fetchval = AsyncMock(return_value=revision)
    ctx.db.acquire = MagicMock(side_effect=AssertionError("a refusal must not touch the pool"))
    return ctx


@pytest.fixture(autouse=True)
def _no_safety_export(monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setattr(
        restore, "write_safety_export",
        AsyncMock(side_effect=AssertionError("a refusal must not write a safety export")),
    )


def test_decode_inverts_encode() -> None:
    when = datetime.datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=datetime.UTC)
    naive = datetime.datetime(2026, 1, 2, 3, 4, 5)
    uid = uuid.uuid4()
    cases = [
        (b"\x00\xff", "bytea"), (when, "timestamp with time zone"),
        (naive, "timestamp without time zone"), (datetime.date(2026, 1, 2), "date"),
        (datetime.time(1, 2, 3), "time without time zone"),
        (decimal.Decimal("1.50"), "numeric"), (uid, "uuid"), (7, "bigint"), ("x", "text"),
        (True, "boolean"), ([b"a", b"b"], "bytea[]"), (None, "text"), ([[1, 2], [3]], "bigint[]"),
    ]
    for value, pg_type in cases:
        assert restore._decode(export._encode(value, pg_type), pg_type) == value, pg_type
    assert json.loads(restore._decode(export._encode('{"a": [1]}', "jsonb"), "jsonb")) == {"a": [1]}
    assert restore._decode(base64.b64encode(b"x").decode(), "bytea") == b"x"


async def test_refuses_corrupt_gzip() -> None:
    result = await restore.import_database(_ctx(), b"not gzip")
    assert not result.ok and result.message == texts.IMPORT_UNREADABLE


async def test_refuses_bad_json() -> None:
    result = await restore.import_database(_ctx(), gzip.compress(b"{nope"))
    assert not result.ok and result.message == texts.IMPORT_UNREADABLE


async def test_refuses_wrong_format_and_version() -> None:
    result = await restore.import_database(_ctx(), _file(format="other"))
    assert not result.ok and result.message == texts.IMPORT_NOT_A_BACKUP
    result = await restore.import_database(_ctx(), _file(version=2))
    assert not result.ok and "2" in result.message


async def test_refuses_malformed_table() -> None:
    bad = {"t": {"columns": [{"name": "a", "type": "bigint"}], "rows": [[1, 2]]}}
    result = await restore.import_database(_ctx(), _file(tables=bad))
    assert not result.ok and result.message == texts.IMPORT_UNREADABLE


@pytest.mark.parametrize("file_revision", ["0099", "0001"])
async def test_refuses_other_revision_naming_both(file_revision: str) -> None:
    ctx = _ctx("0017")
    result = await restore.import_database(ctx, _file(alembic_revision=file_revision))
    assert not result.ok
    assert file_revision in result.message and "0017" in result.message
    assert ctx.maintenance is None
