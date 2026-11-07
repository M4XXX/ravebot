"""Migration 0012 drops the interventions table; downgrade recreates it."""
import importlib.util
import pathlib

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _PATH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain() -> None:
    m = _load("0012_drop_interventions")
    assert m.revision == "0012"
    assert m.down_revision == "0011"


def test_upgrade_drops_and_downgrade_recreates() -> None:
    m6 = _load("0006_interventions")
    m12 = _load("0012_drop_interventions")
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            m6.upgrade()
            assert "interventions" in inspect(conn).get_table_names()
            m12.upgrade()
            assert "interventions" not in inspect(conn).get_table_names()
            m12.downgrade()
            cols = {c["name"] for c in inspect(conn).get_columns("interventions")}
            assert cols == {"discord_user_id", "started_at"}
