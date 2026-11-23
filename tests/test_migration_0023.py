"""Migration 0023: announcement_rules and announcement_posts tables."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0023", _PATH / "0023_announcements.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(op) -> str:
    return "\n".join(c.args[0] for c in op.execute.call_args_list)


def test_chain() -> None:
    m = _load()
    assert (m.revision, m.down_revision) == ("0023", "0022")


def test_0023_upgrade_and_downgrade() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    sql = _sql(op)
    assert "CREATE TABLE announcement_rules" in sql
    assert "guild_id BIGINT NOT NULL" in sql
    assert "days_before INTEGER NOT NULL CHECK (days_before BETWEEN 0 AND 365)" in sql
    assert "post_time TIME NOT NULL" in sql
    assert "created_at TIMESTAMPTZ NOT NULL DEFAULT now()" in sql
    assert "CREATE TABLE announcement_posts" in sql
    assert "CREATE UNIQUE INDEX" in sql
    assert "(event_id, kind, COALESCE(rule_id, 0))" in sql
    assert "REFERENCES" not in sql
    with patch.object(m, "op") as op:
        m.downgrade()
    down = _sql(op)
    assert "DROP TABLE announcement_posts" in down
    assert "DROP TABLE announcement_rules" in down
