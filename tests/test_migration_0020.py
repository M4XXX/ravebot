"""Migration 0020: management_lineup_messages table."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location("m0020", _PATH / "0020_management_lineup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(op) -> str:
    return "\n".join(c.args[0] for c in op.execute.call_args_list)


def test_chain() -> None:
    m = _load()
    assert (m.revision, m.down_revision) == ("0020", "0019")


def test_0020_upgrade_and_downgrade() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    sql = _sql(op)
    assert "CREATE TABLE management_lineup_messages" in sql
    assert "event_id TEXT PRIMARY KEY" in sql
    assert "channel_id BIGINT NOT NULL" in sql
    assert "message_ids BIGINT[] NOT NULL DEFAULT '{}'" in sql
    assert "content_hash TEXT NOT NULL DEFAULT ''" in sql
    assert "updated_at" in sql
    with patch.object(m, "op") as op:
        m.downgrade()
    assert "DROP TABLE management_lineup_messages" in _sql(op)
