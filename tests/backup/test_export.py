"""Unit checks of the backup export helpers (no database)."""
import base64
import datetime
import gzip
import json
from pathlib import Path

from bot.backup import export


def test_encode_values() -> None:
    assert export._encode(b"\x00\xff", "bytea") == base64.b64encode(b"\x00\xff").decode()
    when = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    assert export._encode(when, "timestamp with time zone") == when.isoformat()
    assert export._encode(datetime.date(2026, 1, 2), "date") == "2026-01-02"
    assert export._encode('{"a": [1]}', "jsonb") == {"a": [1]}
    assert export._encode(None, "jsonb") is None
    assert export._encode([b"x"], "bytea[]") == [base64.b64encode(b"x").decode()]
    assert export._encode(7, "bigint") == 7


async def test_safety_export_rotates_to_ten(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    async def fake_export(pool):  # noqa: ANN001, ANN202
        return gzip.compress(json.dumps({"format": export.FORMAT}).encode())

    monkeypatch.setattr(export, "export_database", fake_export)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    (tmp_path / "unrelated.txt").write_text("keep")
    for i in range(12):
        (tmp_path / f"ravebot-20200101-0000{i:02d}-old{export._SUFFIX}").write_bytes(b"x")
    path = await export.write_safety_export(None, "upgrade run")
    assert path.parent == tmp_path
    assert path.name.endswith("-upgrade-run.ravebot.json.gz")
    files = sorted(p.name for p in tmp_path.glob("ravebot-*"))
    assert len(files) == 10 and path.name in files
    assert files[0] == f"ravebot-20200101-000003-old{export._SUFFIX}"
    assert (tmp_path / "unrelated.txt").exists()
    assert json.loads(gzip.decompress(path.read_bytes()))["format"] == export.FORMAT


async def test_safety_export_falls_back_when_unwritable(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    async def fake_export(pool):  # noqa: ANN001, ANN202
        return b"x"

    monkeypatch.setattr(export, "export_database", fake_export)
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    monkeypatch.setenv("BACKUP_DIR", str(blocker / "sub"))
    monkeypatch.chdir(tmp_path)
    path = await export.write_safety_export(None, "x")
    assert path.parent == Path("backups")
    assert (tmp_path / "backups" / path.name).exists()
