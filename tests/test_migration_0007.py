"""Migration 0007 clears the stored Notion DJ page ids."""
import importlib.util
from pathlib import Path
from unittest.mock import patch


def test_migration_0007_clears_notion_dj_page_ids():
    path = Path(__file__).parents[1] / "migrations/versions/0007_clear_notion_dj_page_ids.py"
    spec = importlib.util.spec_from_file_location("mig0007", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0007", "0006")
    with patch.object(mod, "op") as op:
        mod.upgrade()
    op.execute.assert_called_once_with("UPDATE djs SET notion_dj_page_id = NULL")
    with patch.object(mod, "op") as op:
        mod.downgrade()
    assert not op.method_calls
