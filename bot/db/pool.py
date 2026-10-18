"""asyncpg connection pool setup and Alembic migration runner.

Called once at startup in bot/main.py before bot.start():
  1. run_migrations(postgres_url) — subprocess: alembic upgrade head
  2. ctx.db = await create_pool(config)  — opens asyncpg pool

Migration runner uses subprocess to avoid event-loop conflicts with Alembic's
internal asyncio.run() call in migrations/env.py.

Runtime DB access uses the returned asyncpg pool via BotContext.db.
SQLAlchemy is NOT used at runtime — only in migrations/env.py.

[Source: architecture.md#Data Architecture — Schema Migrations: Run-on-Startup]
[Source: architecture.md#asyncpg Connection Pool: Configurable min/max]
[Source: story 1-2-database-pool-migrations-config-table.md]
"""
import asyncio
import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import asyncpg

from bot import redact

if TYPE_CHECKING:
    from bot.config import Config

logger = logging.getLogger(__name__)



def _safe_target(dsn: str) -> str:
    """host:port/dbname of a DSN — never the user or password."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(dsn)
        return f"{parts.hostname}:{parts.port or 5432}{parts.path}"
    except ValueError:
        return "(unparseable)"

async def run_migrations(postgres_url: str) -> None:
    """Run Alembic migrations via subprocess before the asyncpg pool is opened.

    Invokes `python -m alembic upgrade head` as a subprocess so that
    migrations/env.py can safely call asyncio.run() without conflicting
    with the already-running event loop in bot/main.py.

    Exits with a descriptive message if migrations fail (e.g. DB unreachable,
    schema conflict, or Alembic misconfiguration).

    Args:
        postgres_url: The PostgreSQL connection string from POSTGRES_URL env var.
    """
    logger.info("Running database migrations (alembic upgrade head)...")
    # Run in thread executor so the event loop is not blocked while the
    # subprocess executes (correct async pattern even for startup-only code).
    env = {**os.environ, "POSTGRES_URL": postgres_url}
    result = await asyncio.to_thread(
        subprocess.run,  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        secrets = redact.secret_values(postgres_url)
        stdout = redact.redact(result.stdout or "", secrets)
        stderr = redact.redact(result.stderr or "", secrets)
        error_detail = stderr.strip() or stdout.strip()
        logger.error("Migration failed:\nstdout: %s\nstderr: %s", stdout, stderr)
        sys.exit(
            f"Database migration failed — is PostgreSQL reachable?\n"
            f"Check POSTGRES_URL and ensure the database exists.\n"
            f"Error: {error_detail}"
        )
    logger.info("Migrations complete.")


async def create_pool(config: "Config") -> asyncpg.Pool:
    """Create and return a configured asyncpg connection pool.

    Pool sizing is sourced from config (DB_POOL_MIN / DB_POOL_MAX env vars,
    defaulting to min=5 / max=20 as per architecture spec).

    Exits with a descriptive error if PostgreSQL is unreachable or the
    database does not exist — bot cannot operate without a DB connection.

    Args:
        config: Immutable bot configuration loaded from env vars.

    Returns:
        An open asyncpg Pool ready for use via BotContext.db.

    [Source: architecture.md#asyncpg Connection Pool: Configurable min/max]
    """
    logger.info(
        "Opening asyncpg connection pool (min=%d, max=%d)...",
        config.db_pool_min,
        config.db_pool_max,
    )
    try:
        pool = await asyncpg.create_pool(
            dsn=config.postgres_url,
            min_size=config.db_pool_min,
            max_size=config.db_pool_max,
        )
    except (
        OSError,
        ValueError,  # malformed DSN
        asyncpg.PostgresConnectionError,
        asyncpg.PostgresError,  # e.g. InvalidPasswordError, InvalidCatalogNameError
    ) as exc:
        sys.exit(
            f"Failed to connect to PostgreSQL — cannot start bot.\n"
            f"Check POSTGRES_URL (host {_safe_target(config.postgres_url)})\n"
            f"Error: {redact.redact(str(exc), redact.secret_values(config.postgres_url))}"
        )
    if pool is None:
        sys.exit(
            "asyncpg.create_pool() returned None unexpectedly — "
            "check POSTGRES_URL and database availability."
        )
    logger.info("asyncpg pool opened successfully.")
    return pool
