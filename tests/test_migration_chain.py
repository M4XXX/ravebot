"""The migration files form one straight chain with a single head."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))


def test_single_head_and_every_file_is_in_the_chain() -> None:
    script = _script()
    assert script.get_heads() == ["0016"]
    chain = [rev.revision for rev in script.walk_revisions()]
    files = sorted(p.name[:4] for p in (ROOT / "migrations" / "versions").glob("[0-9]*.py"))
    assert sorted(chain) == files
    assert chain[-1] == "0001"  # walk goes from head down to the base


def test_0016_creates_guest_sequence_and_name_index() -> None:
    import importlib
    from unittest.mock import patch

    module = importlib.import_module("migrations.versions.0016_guest_ids_and_constraints")
    with patch.object(module, "op") as op:
        module.upgrade()
    sql = " ".join(str(c.args[0]) for c in op.execute.call_args_list)
    assert "CREATE SEQUENCE guest_dj_id_seq INCREMENT BY -1" in sql
    assert "CREATE UNIQUE INDEX ux_djs_dj_name_norm" in sql
    assert "NOT VALID" in sql
    assert "DROP INDEX IF EXISTS ix_djs_discord_user_id" in sql
