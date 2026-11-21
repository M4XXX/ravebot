"""Migration 0019: event location, hand markers and the schedule_rules table."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0019", _PATH / "0019_event_location_and_rules.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(op) -> str:
    return "\n".join(c.args[0] for c in op.execute.call_args_list)


def test_chain() -> None:
    m = _load()
    assert (m.revision, m.down_revision) == ("0019", "0018")


def test_0019_upgrade_and_downgrade() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    sql = _sql(op)
    assert "ALTER TABLE events ADD COLUMN location TEXT NULL" in sql
    assert "genre_by_hand BOOLEAN NOT NULL DEFAULT false" in sql
    assert "location_by_hand BOOLEAN NOT NULL DEFAULT false" in sql
    assert "UPDATE events SET genre_by_hand = true WHERE genre IS NOT NULL" in sql
    assert "CREATE TABLE schedule_rules" in sql
    with patch.object(m, "op") as op:
        m.downgrade()
    sql = _sql(op)
    assert "DROP TABLE schedule_rules" in sql
    assert "ALTER TABLE events DROP COLUMN location" in sql
