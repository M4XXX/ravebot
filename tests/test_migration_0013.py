"""Migration 0013: DJ logo columns and dj_conversations table."""
import importlib.util
import pathlib
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location("m0013", _PATH / "0013_dj_profile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain() -> None:
    m = _load()
    assert m.revision == "0013"
    assert m.down_revision == "0012"


def test_upgrade_adds_columns_and_table() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    cols = [c.args[1].name for c in op.add_column.call_args_list]
    assert cols == ["logo_image", "logo_synced"]
    assert all(c.args[0] == "djs" for c in op.add_column.call_args_list)
    assert op.create_table.call_args.args[0] == "dj_conversations"


def test_downgrade_drops_both() -> None:
    m = _load()
    with patch.object(m, "op") as op:
        m.downgrade()
    op.drop_table.assert_called_once_with("dj_conversations")
    dropped = [c.args for c in op.drop_column.call_args_list]
    assert ("djs", "logo_synced") in dropped and ("djs", "logo_image") in dropped
