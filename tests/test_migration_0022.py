"""Migration 0022: dm_menus table."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0022", _PATH / "0022_dm_menus.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(op) -> str:
    return "\n".join(c.args[0] for c in op.execute.call_args_list)


def test_chain() -> None:
    m = _load()
    assert (m.revision, m.down_revision) == ("0022", "0021")


def test_0022_upgrade_and_downgrade() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    sql = _sql(op)
    assert "CREATE TABLE dm_menus" in sql
    assert "user_id BIGINT PRIMARY KEY" in sql
    assert "channel_id BIGINT NOT NULL" in sql
    assert "message_id BIGINT NOT NULL" in sql
    assert "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()" in sql
    with patch.object(m, "op") as op:
        m.downgrade()
    assert "DROP TABLE dm_menus" in _sql(op)
