"""Migration 0014: the web_admins table."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location("m0014", _PATH / "0014_web_admins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain() -> None:
    m = _load()
    assert m.revision == "0014"
    assert m.down_revision == "0013"


def test_upgrade_creates_table() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    assert op.create_table.call_args.args[0] == "web_admins"
    names = [c.name for c in op.create_table.call_args.args[1:] if hasattr(c, "autoincrement")]
    assert names == ["discord_user_id", "added_at"]


def test_downgrade_drops_table() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.downgrade()
    op.drop_table.assert_called_once_with("web_admins")


def test_user_id_is_not_a_serial() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    columns = {
        c.name: c for c in op.create_table.call_args.args[1:] if hasattr(c, "autoincrement")
    }
    assert columns["discord_user_id"].autoincrement is False
    assert columns["added_at"].nullable is False
