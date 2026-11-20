"""Migration 0018: drop djs.notion_dj_page_id and the event_shapes table."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0018", _PATH / "0018_drop_notion_page_columns.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(op) -> str:
    return "\n".join(c.args[0] for c in op.execute.call_args_list)


def test_chain() -> None:
    m = _load()
    assert m.revision == "0018"
    assert m.down_revision == "0017"


def test_0018_drops_columns_and_downgrade_restores_page_ids() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    sql = _sql(op)
    assert "ALTER TABLE djs DROP COLUMN notion_dj_page_id" in sql
    assert "DROP TABLE event_shapes" in sql
    with patch.object(m, "op") as op:
        m.downgrade()
    sql = _sql(op)
    assert "CREATE TABLE event_shapes" in sql
    assert "ALTER TABLE djs ADD COLUMN notion_dj_page_id TEXT NULL" in sql
    assert "FROM notion_mirror" in sql
    assert "m.entity = 'dj'" in sql
    assert "m.entity_key = djs.discord_user_id::text" in sql
