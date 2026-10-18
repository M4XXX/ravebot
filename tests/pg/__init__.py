"""Opt-in real-SQL tests; shared settings and the guard against non-test databases."""
import os
import subprocess
import sys
from urllib.parse import urlparse

import pytest

URL = os.environ.get("RAVEBOT_TEST_POSTGRES_URL")

SKIP_WITHOUT_URL = pytest.mark.skipif(
    not URL, reason="RAVEBOT_TEST_POSTGRES_URL not set (opt-in real-SQL tests)"
)


def run_alembic(*args: str) -> None:
    database = urlparse(URL or "").path.lstrip("/")
    if not database.endswith("_test"):
        pytest.exit(f"refusing to drop the schema of '{database}': the name must end in _test")
    env = {**os.environ, "POSTGRES_URL": URL or ""}
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env,
                   capture_output=True, text=True)
