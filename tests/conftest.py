"""Shared pytest fixtures for all RaveBot tests.

Fixtures are added here as stories are implemented. Import in individual
test files is automatic via pytest conftest discovery.
"""
from unittest.mock import MagicMock

import pytest

from bot.config import Config


def make_real_ctx(db: object = None) -> object:
    """Return a real BotContext so get_user_lock() works correctly.

    Used in Story 2.x tests that exercise per-user lock behaviour.
    """
    from bot.context import BotContext

    cfg = MagicMock(spec=Config)
    return BotContext(config=cfg, db=db)


@pytest.fixture
def mock_config() -> Config:
    """Minimal valid Config for unit tests — no real DB or Discord connections."""
    return Config(
        discord_bot_token="test_token",
        postgres_url="postgresql://localhost/test",
        notion_api_key="secret_test",
        notion_parent_page_id="test-parent-page-id",
        admin_user_ids=frozenset({111}),
        log_level="INFO",
        db_pool_min=2,
        db_pool_max=5,
    )


@pytest.fixture(autouse=True)
def _bot_is_active(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests exercise the active bot: report the activation state as active.

    Tests of the activation logic itself (test_activation) use the real functions;
    idle behaviour is tested by overriding these patches in the test.
    """
    if request.module.__name__.endswith("test_activation"):
        return
    from unittest.mock import AsyncMock

    from bot.services import activation

    monkeypatch.setattr(activation, "is_active", AsyncMock(return_value=True))
    monkeypatch.setattr(activation, "bot_is_active", AsyncMock(return_value=True))
