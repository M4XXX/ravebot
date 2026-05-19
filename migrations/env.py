"""Alembic async env.py — reads POSTGRES_URL from environment, uses asyncpg driver.

SQLAlchemy is used here for migration-time connection management ONLY.
The asyncpg pool used at runtime (bot/db/pool.py) is completely separate.

[Source: architecture.md#Data Architecture — Schema Migrations: Run-on-Startup]
[Source: story 1-2-database-pool-migrations-config-table.md#Task 1]
"""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Alembic Config object — provides access to alembic.ini values.
config = context.config

# Set up Python logging from alembic.ini config (if present).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy ORM metadata — we use raw op.create_table() in migrations.
target_metadata = None


def get_url() -> str:
    """Read POSTGRES_URL from environment and convert to asyncpg driver scheme.

    asyncpg requires the postgresql+asyncpg:// scheme.
    Accepts both postgresql:// and postgres:// (Heroku-style) prefixes.
    """
    url = os.environ["POSTGRES_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    # Already has correct scheme or non-standard — return as-is.
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection, generates SQL).

    Used when running: alembic upgrade head --sql
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    """Configure Alembic context with a live connection and run pending migrations."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async SQLAlchemy engine.

    Creates a temporary async engine, runs all pending migrations, then
    disposes the engine. The engine is NOT reused at runtime.
    """
    # NullPool: each connection is discarded immediately — correct for a one-shot migration runner.
    engine = create_async_engine(get_url(), poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
