"""Opt-in real-SQL tests against a throwaway PostgreSQL database.

Run them with a database whose name ends in _test (the suite drops its schema):

    RAVEBOT_TEST_POSTGRES_URL=postgresql://ravebot@localhost:5432/ravebot_test \
        uv run pytest -m pg -q

Without the variable every test here is skipped.
"""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

URL = os.environ.get("RAVEBOT_TEST_POSTGRES_URL")
ROOT = Path(__file__).resolve().parents[2]


def run_alembic(*args: str) -> None:
    database = urlparse(URL or "").path.lstrip("/")
    if not database.endswith("_test"):
        pytest.exit(f"refusing to drop the schema of '{database}': the name must end in _test")
    env = {**os.environ, "POSTGRES_URL": URL or ""}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *args],
        check=True, env=env, capture_output=True, text=True, cwd=ROOT,
    )
