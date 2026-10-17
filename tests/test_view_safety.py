"""A click only ever acts on the clicking DJ's own, current menu."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.context import FlowLock
from bot.ui.views import (
    NOT_YOUR_MENU_TEXT,
    STALE_MENU_TEXT,
    FallbackMenuView,
    SlotSelectionView,
)

EVENT = {"id": "evt", "properties": {"Date": {"date": {"start": "2026-10-24"}}}}


async def _slot_view(user_id: int) -> tuple[SlotSelectionView, FlowLock]:
    lock = FlowLock()
    await lock.acquire()
    return SlotSelectionView(MagicMock(), EVENT, [0, 1], lock, user_id), lock


def _click(author_id: int) -> MagicMock:
    inter = MagicMock()
    inter.author.id = author_id
    inter.response.send_message = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_menus_of_different_djs_never_share_custom_ids() -> None:
    a, _ = await _slot_view(111)
    b, _ = await _slot_view(222)
    ids_a = {item.custom_id for item in a.children}
    ids_b = {item.custom_id for item in b.children}
    assert ids_a.isdisjoint(ids_b)


@pytest.mark.asyncio
async def test_persistent_menus_keep_static_ids() -> None:
    one, two = FallbackMenuView(MagicMock()), FallbackMenuView(MagicMock())
    assert [i.custom_id for i in one.children] == [i.custom_id for i in two.children]


@pytest.mark.asyncio
async def test_other_dj_cannot_use_my_menu() -> None:
    view, _ = await _slot_view(111)
    inter = _click(222)
    assert await view.interaction_check(inter) is False
    inter.response.send_message.assert_awaited_once_with(NOT_YOUR_MENU_TEXT, ephemeral=True)


@pytest.mark.asyncio
async def test_menu_is_single_use() -> None:
    view, _ = await _slot_view(111)
    assert await view.interaction_check(_click(111)) is True
    second = _click(111)
    assert await view.interaction_check(second) is False
    second.response.send_message.assert_awaited_once_with(STALE_MENU_TEXT, ephemeral=True)


@pytest.mark.asyncio
async def test_menu_of_an_older_flow_is_inert_and_cannot_free_the_new_lock() -> None:
    view, lock = await _slot_view(111)
    lock.release()
    await lock.acquire()  # the DJ started a new flow
    inter = _click(111)
    assert await view.interaction_check(inter) is False
    inter.response.send_message.assert_awaited_once_with(STALE_MENU_TEXT, ephemeral=True)
    view._release_lock()
    assert lock.locked() is True
