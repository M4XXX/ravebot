"""Tests for bot/services/admin_registry.py."""
from unittest.mock import AsyncMock, patch

import pytest

from bot.services.admin_registry import AdminRegistry

DB = object()


def _registry(owners=frozenset({30, 10})) -> AdminRegistry:
    return AdminRegistry(owners, DB)


def test_owners_listed_first_and_sorted() -> None:
    r = _registry()
    assert r.all_ids() == [10, 30]
    assert r.owners == frozenset({10, 30})
    assert r.added == frozenset()
    assert r.is_owner(10) and r.is_admin(30) and not r.is_admin(99)


@pytest.mark.asyncio
async def test_load_reads_the_table() -> None:
    r = _registry()
    with patch("bot.services.admin_registry.list_web_admins", AsyncMock(return_value=[7, 5])):
        await r.load()
    assert r.all_ids() == [10, 30, 7, 5]
    assert r.added == frozenset({7, 5})


@pytest.mark.asyncio
async def test_load_failure_keeps_owners_only_and_does_not_raise() -> None:
    r = _registry()
    with patch(
        "bot.services.admin_registry.list_web_admins", AsyncMock(side_effect=RuntimeError("db"))
    ):
        await r.load()
    assert r.all_ids() == [10, 30]


@pytest.mark.asyncio
async def test_add_owner_or_duplicate_returns_false_without_db_call() -> None:
    r = _registry()
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)) as add:
        assert await r.add(10) is False
        assert await r.add(7) is True
        add.reset_mock()
        assert await r.add(7) is False
        add.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_caches_after_success_and_is_admin_immediately() -> None:
    r = _registry()
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)) as add:
        assert await r.add(7) is True
    add.assert_awaited_once_with(DB, 7)
    assert r.is_admin(7) and not r.is_owner(7)
    assert r.all_ids() == [10, 30, 7]


@pytest.mark.asyncio
async def test_add_db_error_leaves_cache_unchanged_and_propagates() -> None:
    r = _registry()
    with patch(
        "bot.services.admin_registry.add_web_admin", AsyncMock(side_effect=RuntimeError("db"))
    ):
        with pytest.raises(RuntimeError):
            await r.add(7)
    assert not r.is_admin(7) and r.added == frozenset()


@pytest.mark.asyncio
async def test_remove_owner_or_unknown_returns_false_without_db_call() -> None:
    r = _registry()
    with patch(
        "bot.services.admin_registry.remove_web_admin", AsyncMock(return_value=True)
    ) as rm:
        assert await r.remove(10) is False
        assert await r.remove(99) is False
        rm.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_added_admin_deletes_and_updates_cache() -> None:
    r = _registry()
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)):
        await r.add(7)
    with patch(
        "bot.services.admin_registry.remove_web_admin", AsyncMock(return_value=True)
    ) as rm:
        assert await r.remove(7) is True
    rm.assert_awaited_once_with(DB, 7)
    assert not r.is_admin(7)


@pytest.mark.asyncio
async def test_remove_db_error_propagates_and_keeps_cache() -> None:
    r = _registry()
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)):
        await r.add(7)
    with patch(
        "bot.services.admin_registry.remove_web_admin", AsyncMock(side_effect=RuntimeError("db"))
    ):
        with pytest.raises(RuntimeError):
            await r.remove(7)
    assert r.is_admin(7)


@pytest.mark.asyncio
async def test_loaded_flag_and_ensure_loaded() -> None:
    r = _registry()
    assert r.loaded is False
    failing = AsyncMock(side_effect=RuntimeError("db"))
    with patch("bot.services.admin_registry.list_web_admins", failing):
        await r.load()
        assert r.loaded is False
        await r.ensure_loaded()
        assert failing.await_count == 2
        assert r.loaded is False
    with patch("bot.services.admin_registry.list_web_admins", AsyncMock(return_value=[7])):
        await r.ensure_loaded()
    assert r.loaded is True and r.added == frozenset({7})
    again = AsyncMock(return_value=[8])
    with patch("bot.services.admin_registry.list_web_admins", again):
        await r.ensure_loaded()
    again.assert_not_awaited()
    assert r.added == frozenset({7})


@pytest.mark.asyncio
async def test_add_existing_row_still_caches_but_returns_false() -> None:
    r = _registry()
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=False)):
        assert await r.add(55) is False
    assert r.is_admin(55)


class _SlowDb:
    """list_web_admins replacement: returns the snapshot taken at call time, after a delay."""

    def __init__(self, rows: list[int]) -> None:
        self.rows = rows
        self.calls = 0

    async def __call__(self, db):
        import asyncio

        self.calls += 1
        snapshot = list(self.rows)
        await asyncio.sleep(0.05)
        return snapshot


@pytest.mark.asyncio
async def test_concurrent_ensure_loaded_loads_once() -> None:
    import asyncio

    r = _registry()
    slow = _SlowDb([7])
    with patch("bot.services.admin_registry.list_web_admins", slow):
        await asyncio.gather(r.ensure_loaded(), r.ensure_loaded())
    assert slow.calls == 1
    assert r.added == frozenset({7})


@pytest.mark.asyncio
async def test_concurrent_loads_do_not_overwrite_newer_with_older() -> None:
    import asyncio

    r = _registry()
    slow = _SlowDb([7])

    async def change_rows() -> None:
        await asyncio.sleep(0.02)
        slow.rows = [7, 8]

    with patch("bot.services.admin_registry.list_web_admins", slow):
        await asyncio.gather(r.load(), change_rows(), r.load())
    # the second load ran after the first finished and saw the newer rows
    assert r.added == frozenset({7, 8})


@pytest.mark.asyncio
async def test_add_during_a_load_is_not_lost() -> None:
    import asyncio

    r = _registry()
    slow = _SlowDb([7])
    with patch("bot.services.admin_registry.list_web_admins", slow), \
         patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)):
        await asyncio.gather(r.load(), r.add(9))
    assert r.added == frozenset({7, 9})
