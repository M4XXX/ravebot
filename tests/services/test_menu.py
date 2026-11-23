"""Tests for bot/services/menu.py: the DM menu after flows."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.db.dm_menus import LastMenu
from bot.services.menu import MenuService
from bot.ui import texts

USER = 42


def _ctx(locked: bool = False, conversation_open: bool = False):
    ctx = MagicMock()
    ctx.db = MagicMock()
    lock = MagicMock()
    lock.locked.return_value = locked
    ctx.get_user_lock.return_value = lock
    ctx.dj_conversations = MagicMock(
        has_open_conversation=AsyncMock(return_value=conversation_open)
    )
    return ctx


def _channel(sent_id: int = 500):
    channel = MagicMock(id=7)
    sent = MagicMock(id=sent_id)
    sent.channel.id = 7
    channel.send = AsyncMock(return_value=sent)
    partial = MagicMock(delete=AsyncMock())
    channel.get_partial_message = MagicMock(return_value=partial)
    return channel, partial


def _patches(old: LastMenu | None = None, active: bool = True, dj: dict | None = None):
    store = {"get": AsyncMock(return_value=old), "set": AsyncMock()}
    return store, [
        patch("bot.services.menu.dm_menus.get_menu", new=store["get"]),
        patch("bot.services.menu.dm_menus.set_menu", new=store["set"]),
        patch("bot.services.menu.get_dj_by_discord_id", new=AsyncMock(return_value=dj)),
        patch("bot.services.menu.activation.bot_is_active", new=AsyncMock(return_value=active)),
    ]


async def _with(patches, coro):
    for p in patches:
        p.start()
    try:
        return await coro
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_post_now_sends_menu_and_stores_id() -> None:
    svc = MenuService(MagicMock(), _ctx())
    channel, _ = _channel()
    store, ps = _patches()
    await _with(ps, svc.post_now(channel, USER))
    assert channel.send.await_args.args[0] == texts.MENU
    store["set"].assert_awaited_once_with(svc.ctx.db, USER, 7, 500)


@pytest.mark.asyncio
async def test_post_now_deletes_old_menu() -> None:
    svc = MenuService(MagicMock(), _ctx())
    channel, partial = _channel()
    _, ps = _patches(old=LastMenu(USER, 7, 111))
    await _with(ps, svc.post_now(channel, USER))
    channel.get_partial_message.assert_called_once_with(111)
    partial.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_menu_already_deleted_is_ignored() -> None:
    svc = MenuService(MagicMock(), _ctx())
    channel, partial = _channel()
    partial.delete = AsyncMock(side_effect=disnake.NotFound(MagicMock(status=404), "gone"))
    store, ps = _patches(old=LastMenu(USER, 7, 111))
    await _with(ps, svc.post_now(channel, USER))
    store["set"].assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_send_keeps_old_record() -> None:
    svc = MenuService(MagicMock(), _ctx())
    channel, partial = _channel()
    channel.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "no"))
    store, ps = _patches(old=LastMenu(USER, 7, 111))
    with pytest.raises(disnake.Forbidden):
        await _with(ps, svc.post_now(channel, USER))
    store["set"].assert_not_awaited()
    partial.delete.assert_not_awaited()


def _scheduled(ctx, channel, delay=0.01, active=True):
    bot = MagicMock()
    user = MagicMock(create_dm=AsyncMock(return_value=channel))
    bot.get_user.return_value = user
    return MenuService(bot, ctx, delay=delay)


@pytest.mark.asyncio
async def test_schedule_sends_after_delay_and_is_debounced() -> None:
    channel, _ = _channel()
    svc = _scheduled(_ctx(), channel)
    _, ps = _patches()
    for p in ps:
        p.start()
    try:
        svc.schedule(USER)
        svc.schedule(USER)
        await asyncio.sleep(0.1)
    finally:
        for p in ps:
            p.stop()
    assert channel.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"locked": True}, {"conversation_open": True}])
async def test_schedule_skips_while_user_is_busy(kwargs) -> None:
    channel, _ = _channel()
    svc = _scheduled(_ctx(**kwargs), channel)
    _, ps = _patches()
    for p in ps:
        p.start()
    try:
        svc.schedule(USER)
        await asyncio.sleep(0.1)
    finally:
        for p in ps:
            p.stop()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_skips_when_bot_idle_and_for_guests() -> None:
    channel, _ = _channel()
    svc = _scheduled(_ctx(), channel)
    _, ps = _patches(active=False)
    for p in ps:
        p.start()
    try:
        svc.schedule(USER)
        svc.schedule(-5)
        await asyncio.sleep(0.1)
    finally:
        for p in ps:
            p.stop()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_now_cancels_pending_menu() -> None:
    channel, _ = _channel()
    svc = _scheduled(_ctx(), channel, delay=0.05)
    _, ps = _patches()
    for p in ps:
        p.start()
    try:
        svc.schedule(USER)
        await svc.post_now(channel, USER)
        await asyncio.sleep(0.15)
    finally:
        for p in ps:
            p.stop()
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_scheduled_menu_rechecks_the_lock_before_sending() -> None:
    ctx = _ctx(locked=True)  # a flow took the lock while the menu was on its way
    channel, _ = _channel()
    svc = _scheduled(ctx, channel, delay=0.0)
    _, ps = _patches()
    await _with(ps, svc._post(channel, USER, recheck=True))
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_menus_at_once_leave_only_the_newest() -> None:
    svc = MenuService(MagicMock(), _ctx())
    channel, partial = _channel()
    ids = iter([501, 502])
    sent = []

    async def _send(*a, **k):
        await asyncio.sleep(0.01)
        m = MagicMock(id=next(ids))
        m.channel.id = 7
        sent.append(m)
        return m

    channel.send = AsyncMock(side_effect=_send)
    state = {"old": None}

    async def _get(db, uid):
        return state["old"]

    async def _set(db, uid, cid, mid):
        state["old"] = LastMenu(uid, cid, mid)

    _, ps = _patches()
    ps += [
        patch("bot.services.menu.dm_menus.get_menu", new=_get),
        patch("bot.services.menu.dm_menus.set_menu", new=_set),
    ]
    await _with(ps, asyncio.gather(svc.post_now(channel, USER), svc.post_now(channel, USER)))
    assert channel.get_partial_message.call_args_list[0].args == (501,)
