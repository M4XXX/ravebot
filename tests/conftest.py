"""Shared pytest fixtures for all RaveBot tests.

Fixtures are added here as stories are implemented. Import in individual
test files is automatic via pytest conftest discovery.
"""
import pytest

from bot.config import Config


@pytest.fixture
def mock_config() -> Config:
    """Minimal valid Config for unit tests — no real DB or Discord connections."""
    return Config(
        discord_bot_token="test_token",
        postgres_url="postgresql://localhost/test",
        notion_api_key="secret_test",
        notion_database_id="test-database-id",
        admin_user_ids=frozenset({111}),
        log_level="INFO",
        db_pool_min=2,
        db_pool_max=5,
    )
