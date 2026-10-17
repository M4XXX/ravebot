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
        notion_database_id="test-database-id",
        admin_user_ids=frozenset({111}),
        log_level="INFO",
        db_pool_min=2,
        db_pool_max=5,
    )


@pytest.fixture(autouse=True)
def _bot_active_and_serving(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most tests exercise the active bot in its one served guild.

    Tests marked @pytest.mark.passive skip this and use the real switch and guild lookup
    (default: passive, see bot/active.py and bot/guild.py).
    """
    if request.node.get_closest_marker("passive"):
        return
    from unittest.mock import AsyncMock

    monkeypatch.setattr("bot.active.is_active", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.guild.serves", lambda bot, guild_id: True)


@pytest.fixture(autouse=True)
def _slot_state_from_notion_count(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older tests describe bookings only through the Notion DJs relation.

    Leaving events un-annotated makes bot.slots.state() fall back to that count, i.e. these
    tests run as if slot_locks agreed with Notion. Tests marked @pytest.mark.real_slots use
    the real annotate() against a mocked pool.
    """
    if request.node.get_closest_marker("real_slots"):
        return
    from unittest.mock import AsyncMock

    monkeypatch.setattr("bot.slots.annotate", AsyncMock(side_effect=lambda pool, gid, ev: ev))
