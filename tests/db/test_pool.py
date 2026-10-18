"""Tests for bot/db/pool.py — asyncpg pool setup and Alembic migration runner.

[Source: story 1-2-database-pool-migrations-config-table.md]
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db.pool import create_pool, run_migrations


class TestCreatePool:
    """Tests for create_pool() — asyncpg pool initialisation."""

    @pytest.mark.asyncio
    async def test_creates_pool_with_config_sizes(self, mock_config):
        """Pool is created using min_size and max_size from config (AC: 3)."""
        mock_pool = MagicMock()
        with patch("asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)) as mock_create:
            result = await create_pool(mock_config)
            mock_create.assert_called_once_with(
                dsn=mock_config.postgres_url,
                min_size=mock_config.db_pool_min,
                max_size=mock_config.db_pool_max,
            )
            assert result is mock_pool

    @pytest.mark.asyncio
    async def test_exits_on_os_error(self, mock_config):
        """OSError (connection refused) causes sys.exit with POSTGRES_URL in message (AC: 4)."""
        with patch("asyncpg.create_pool", new=AsyncMock(side_effect=OSError("Connection refused"))):
            with pytest.raises(SystemExit) as exc_info:
                await create_pool(mock_config)
            assert "POSTGRES_URL" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exits_with_descriptive_message_on_pg_error(self, mock_config):
        """PostgresConnectionError causes sys.exit with descriptive message (AC: 4)."""
        import asyncpg as apg

        with patch(
            "asyncpg.create_pool",
            new=AsyncMock(side_effect=apg.PostgresConnectionError("bad connection")),
        ):
            with pytest.raises(SystemExit) as exc_info:
                await create_pool(mock_config)
            assert "POSTGRES_URL" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exits_when_pool_returns_none(self, mock_config):
        """None return from asyncpg.create_pool triggers sys.exit (defensive guard)."""
        with patch("asyncpg.create_pool", new=AsyncMock(return_value=None)):
            with pytest.raises(SystemExit):
                await create_pool(mock_config)


class TestRunMigrations:
    """Tests for run_migrations() — Alembic subprocess runner."""

    @pytest.mark.asyncio
    async def test_exits_on_nonzero_returncode(self, mock_config):
        """Non-zero subprocess returncode causes sys.exit with descriptive message (AC: 4)."""
        failed = MagicMock()
        failed.returncode = 1
        failed.stderr = "could not connect to server"
        failed.stdout = ""
        with patch("subprocess.run", return_value=failed):
            with pytest.raises(SystemExit) as exc_info:
                await run_migrations(mock_config.postgres_url)
            msg = str(exc_info.value)
            assert "migration" in msg.lower() or "PostgreSQL" in msg

    @pytest.mark.asyncio
    async def test_masks_credentials_in_failure_output(self, caplog):
        """DSN and password never reach the exit message or log records."""
        dsn = "postgresql://ravebot:S3cr3t%40Pw@db.example:5432/ravebot"
        failed = MagicMock()
        failed.returncode = 1
        failed.stderr = f"connection to {dsn} refused (password S3cr3t@Pw)"
        failed.stdout = f"url={dsn} pw=S3cr3t%40Pw"
        with caplog.at_level("DEBUG"):
            with patch("subprocess.run", return_value=failed):
                with pytest.raises(SystemExit) as exc_info:
                    await run_migrations(dsn)
        msg = str(exc_info.value)
        assert "Database migration failed" in msg
        for text in (msg, caplog.text):
            assert "S3cr3t" not in text
            assert dsn not in text

    @pytest.mark.asyncio
    async def test_succeeds_on_zero_returncode(self, mock_config):
        """Zero returncode means migrations ran without error — no exception raised (AC: 1)."""
        success = MagicMock()
        success.returncode = 0
        success.stdout = "INFO  [alembic.runtime.migration] Running upgrade -> 0001"
        with patch("subprocess.run", return_value=success):
            # Should not raise
            await run_migrations(mock_config.postgres_url)

    @pytest.mark.asyncio
    async def test_passes_postgres_url_to_subprocess_env(self, mock_config):
        """POSTGRES_URL is forwarded to the subprocess environment."""
        success = MagicMock()
        success.returncode = 0
        success.stdout = ""
        with patch("subprocess.run", return_value=success) as mock_sub:
            await run_migrations(mock_config.postgres_url)
            call_kwargs = mock_sub.call_args
            env_passed = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env_passed is not None
            assert env_passed["POSTGRES_URL"] == mock_config.postgres_url


class TestCreatePoolMasking:
    @pytest.mark.asyncio
    async def test_connection_error_is_masked(self, mock_config):
        from dataclasses import replace

        config = replace(
            mock_config, postgres_url="postgresql://ravebot:S3cr3t%40Pw@db.example:5432/ravebot"
        )
        err = OSError("cannot reach postgresql://ravebot:S3cr3t%40Pw@db.example:5432/ravebot")
        with patch("asyncpg.create_pool", side_effect=err):
            with pytest.raises(SystemExit) as exc_info:
                await create_pool(config)
        assert "S3cr3t" not in str(exc_info.value)
        assert "db.example:5432/ravebot" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_invalid_password_error_is_masked(self, mock_config):
        """Auth failures (PostgresError, not a connection error) exit with masked text."""
        import asyncpg
        from dataclasses import replace

        config = replace(mock_config, postgres_url="postgresql://ravebot:S3cr3tPw@db.example/rb")
        err = asyncpg.InvalidPasswordError('password authentication failed for user "ravebot"')
        with patch("asyncpg.create_pool", side_effect=err):
            with pytest.raises(SystemExit) as exc_info:
                await create_pool(config)
        msg = str(exc_info.value)
        assert "Failed to connect to PostgreSQL" in msg
        assert 'for user "***"' in msg

    @pytest.mark.asyncio
    async def test_malformed_dsn_value_error_is_masked(self, mock_config):
        from dataclasses import replace

        dsn = "postgresql://ravebot:S3cr3tPw@db.example:notaport/rb"
        config = replace(mock_config, postgres_url=dsn)
        with patch("asyncpg.create_pool", side_effect=ValueError(f"invalid port in {dsn}")):
            with pytest.raises(SystemExit) as exc_info:
                await create_pool(config)
        assert "S3cr3tPw" not in str(exc_info.value)
