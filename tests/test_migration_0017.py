"""Migration 0017: events, notion_mirror, notion_outbox, sync_issues."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location("m0017", _PATH / "0017_postgres_source_of_truth.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(op) -> str:
    return "\n".join(c.args[0] for c in op.execute.call_args_list)


def test_chain() -> None:
    m = _load()
    assert m.revision == "0017"
    assert m.down_revision == "0016"


def test_0017_creates_tables_and_copies_dj_page_ids() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    sql = _sql(op)
    for table in ("events", "notion_mirror", "notion_outbox", "sync_issues"):
        assert f"CREATE TABLE {table} " in sql
    assert "slot_count BETWEEN 1 AND 100" in sql
    assert "WHERE resolved_at IS NULL AND kind = 'conflict'" in sql
    assert "SELECT 'dj', discord_user_id::text, notion_dj_page_id" in sql
    assert "WHERE notion_dj_page_id IS NOT NULL" in sql


def test_downgrade_drops_tables() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.downgrade()
    sql = _sql(op)
    for table in ("events", "notion_mirror", "notion_outbox", "sync_issues"):
        assert f"DROP TABLE {table}" in sql
