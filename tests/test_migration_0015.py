"""Migration 0015: the manual_events table."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location("m0015", _PATH / "0015_manual_events.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain() -> None:
    m = _load()
    assert m.revision == "0015"
    assert m.down_revision == "0014"


def test_upgrade_creates_table() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    assert op.create_table.call_args.args[0] == "manual_events"
    names = [c.name for c in op.create_table.call_args.args[1:] if hasattr(c, "nullable")]
    assert names == ["page_id", "created_at"]


def test_downgrade_drops_table() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.downgrade()
    op.drop_table.assert_called_once_with("manual_events")
