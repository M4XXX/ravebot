"""Tests for bot/context.py — BotContext dataclass."""
import asyncio

import pytest

from bot.config import Config
from bot.context import BotContext


def make_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/test")
    monkeypatch.setenv("NOTION_API_KEY", "secret_key")
    monkeypatch.setenv("NOTION_DATABASE_ID", "test-db-id")
    monkeypatch.setenv("ADMIN_USER_IDS", "1,2")
    return Config.from_env()


class TestBotContext:
    def test_user_locks_initialised_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """user_locks starts as an empty dict."""
        ctx = BotContext(config=make_config(monkeypatch))
        assert ctx.user_locks == {}

    def test_db_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """db pool is None until Story 1.2 initialises it."""
        ctx = BotContext(config=make_config(monkeypatch))
        assert ctx.db is None

    def test_get_user_lock_creates_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_user_lock() creates a new asyncio.Lock for unknown user_id."""
        ctx = BotContext(config=make_config(monkeypatch))
        lock = ctx.get_user_lock(42)
        assert isinstance(lock, asyncio.Lock)

    def test_get_user_lock_returns_same_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_user_lock() returns the same lock object on repeated calls."""
        ctx = BotContext(config=make_config(monkeypatch))
        lock_a = ctx.get_user_lock(42)
        lock_b = ctx.get_user_lock(42)
        assert lock_a is lock_b

    def test_get_user_lock_isolates_per_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Different user_ids get different locks."""
        ctx = BotContext(config=make_config(monkeypatch))
        lock_a = ctx.get_user_lock(1)
        lock_b = ctx.get_user_lock(2)
        assert lock_a is not lock_b

    def test_user_locks_persisted_in_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After get_user_lock(), the lock appears in user_locks dict."""
        ctx = BotContext(config=make_config(monkeypatch))
        ctx.get_user_lock(99)
        assert 99 in ctx.user_locks

    def test_config_accessible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config is stored and accessible on the context."""
        config = make_config(monkeypatch)
        ctx = BotContext(config=config)
        assert ctx.config is config
