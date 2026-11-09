"""Tests for bot/context.py — BotContext dataclass."""
import asyncio

import pytest

from bot.config import Config
from bot.context import BotContext


def make_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/test")
    monkeypatch.setenv("NOTION_API_KEY", "secret_key")
    monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "test-parent-id")
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
        """get_user_lock() creates a new FlowLock for unknown user_id."""
        from bot.context import FlowLock

        ctx = BotContext(config=make_config(monkeypatch))
        lock = ctx.get_user_lock(42)
        assert isinstance(lock, FlowLock)

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


class TestAdminAccess:
    def test_is_admin_falls_back_to_config_without_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = BotContext(config=make_config(monkeypatch))
        assert ctx.admins is None
        assert ctx.is_admin(1) and ctx.is_admin(2) and not ctx.is_admin(3)
        assert ctx.admin_ids() == [1, 2]

    def test_registry_wins_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bot.services.admin_registry import AdminRegistry

        ctx = BotContext(config=make_config(monkeypatch))
        registry = AdminRegistry(frozenset({2}), None)
        registry._added = [9]
        ctx.admins = registry
        assert ctx.is_admin(9) and ctx.is_admin(2) and not ctx.is_admin(1)
        assert ctx.admin_ids() == [2, 9]


class TestFlowLock:
    async def test_acquire_release_and_tokens(self) -> None:
        from bot.context import FlowLock

        lock = FlowLock()
        assert not lock.locked() and lock.token == 0
        await lock.acquire()
        first = lock.token
        assert lock.locked() and lock.owns(first)
        with pytest.raises(RuntimeError):
            await lock.acquire()
        assert lock.release_if(first) and not lock.locked()
        await lock.acquire()
        second = lock.token
        assert second != first
        assert not lock.release_if(first)  # an old flow cannot free the new one
        assert not lock.owns(first) and lock.locked()
        lock.release()
        assert not lock.locked()

    async def test_abandoned_flow_expires(self) -> None:
        from bot.context import FlowLock

        now = [0.0]
        lock = FlowLock(ttl=10, clock=lambda: now[0])
        await lock.acquire()
        token = lock.token
        now[0] = 11
        assert not lock.locked() and not lock.owns(token)
        await lock.acquire()  # a new flow may start
        assert lock.locked() and lock.token != token

    def test_free_locks_are_dropped_when_the_dict_grows(self, monkeypatch) -> None:
        ctx = BotContext(config=make_config(monkeypatch))
        for uid in range(1002):
            ctx.get_user_lock(uid)
        assert len(ctx.user_locks) < 10
