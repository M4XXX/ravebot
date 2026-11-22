"""Tests for bot/services/admin_management.py."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.context import BotContext
from bot.services.admin_management import AdminChange, AdminManagementService
from bot.services.admin_registry import AdminRegistry
from bot.ui import texts

UID = 123456789012345678
SAVE_FAILED = "Could not save the change. Try again."


def _http(cls=disnake.HTTPException):
    resp = MagicMock()
    resp.status = 404
    return cls(resp, "x")


def _user(global_name="Glob", name="raw", bot=False):
    return SimpleNamespace(id=UID, global_name=global_name, name=name, bot=bot)


def _bot(user=None, exc=None, member=None, cached_user=None):
    guild = MagicMock()
    guild.get_member = MagicMock(return_value=member)
    bot = MagicMock()
    bot.guilds = [guild]
    bot.get_user = MagicMock(return_value=cached_user)
    bot.fetch_user = AsyncMock(return_value=user, side_effect=exc)
    return bot


def _ctx(owners=frozenset({10}), added=()):
    reg = AdminRegistry(owners, object())
    reg._added = list(added)
    reg._loaded = True
    ctx = BotContext(config=MagicMock(), db=object(), admins=reg)
    ctx.log_service = MagicMock()
    ctx.log_service.log_admin_added = AsyncMock()
    ctx.log_service.log_admin_removed = AsyncMock()
    return ctx


def _svc(bot=None, ctx=None):
    return AdminManagementService(ctx or _ctx(), bot or _bot(_user()))


def _patch_add(value=True):
    return patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=value))


def _patch_remove(value=True):
    return patch("bot.services.admin_registry.remove_web_admin", AsyncMock(return_value=value))


def _patch_active(value=True):
    return patch("bot.services.admin_management.bot_is_active", AsyncMock(return_value=value))


def test_requires_registry() -> None:
    ctx = BotContext(config=MagicMock(), db=object())
    with pytest.raises(RuntimeError):
        AdminManagementService(ctx, _bot())


async def test_add_success_logs_once_with_actor() -> None:
    ctx = _ctx()
    with _patch_add(), _patch_active():
        res = await _svc(ctx=ctx).add(UID, "Max")
    assert res == AdminChange(True, "Added Glob.")
    assert ctx.admins.is_admin(UID)
    ctx.log_service.log_admin_added.assert_awaited_once_with("Glob", UID, "Max")


async def test_add_name_falls_back_to_id() -> None:
    bot = _bot(_user(global_name="", name=""))
    with _patch_add(), _patch_active():
        res = await _svc(bot=bot).add(UID, None)
    assert res.message == f"Added {UID}."


@pytest.mark.parametrize("who", ["admin", "owner"])
async def test_add_existing_admin_or_owner(who) -> None:
    ctx = _ctx(owners=frozenset({UID if who == "owner" else 10}),
               added=[UID] if who == "admin" else [])
    bot = _bot(_user())
    res = await _svc(bot=bot, ctx=ctx).add(UID, "Max")
    assert res == AdminChange(False, "This person is already an admin.")
    bot.fetch_user.assert_not_awaited()
    ctx.log_service.log_admin_added.assert_not_awaited()


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (_http(disnake.NotFound), "No Discord user with this ID."),
        (_http(), "Could not reach Discord. Try again."),
        (TimeoutError(), "Could not reach Discord. Try again."),
    ],
)
async def test_add_lookup_errors(exc, message) -> None:
    ctx = _ctx()
    with _patch_add() as add, _patch_active():
        res = await _svc(bot=_bot(exc=exc), ctx=ctx).add(UID, "Max")
    assert res == AdminChange(False, message)
    add.assert_not_awaited()
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_add_refuses_bot_accounts() -> None:
    ctx = _ctx()
    with _patch_add() as add, _patch_active():
        res = await _svc(bot=_bot(_user(bot=True)), ctx=ctx).add(UID, "Max")
    assert res == AdminChange(False, "Bots cannot be admins.")
    add.assert_not_awaited()
    assert not ctx.admins.is_admin(UID)
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_add_save_failure_changes_nothing() -> None:
    ctx = _ctx()
    boom = AsyncMock(side_effect=RuntimeError("db"))
    with patch("bot.services.admin_registry.add_web_admin", boom), _patch_active():
        res = await _svc(ctx=ctx).add(UID, "Max")
    assert res == AdminChange(False, SAVE_FAILED)
    assert not ctx.admins.is_admin(UID)
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_add_registry_refuses() -> None:
    ctx = _ctx()
    with _patch_add(False), _patch_active():
        res = await _svc(ctx=ctx).add(UID, "Max")
    assert res == AdminChange(False, "This person is already an admin.")
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_add_log_failure_still_succeeds() -> None:
    ctx = _ctx()
    ctx.log_service.log_admin_added.side_effect = RuntimeError("x")
    with _patch_add(), _patch_active():
        res = await _svc(ctx=ctx).add(UID, "Max")
    assert res.ok


async def test_add_idle_bot_no_log() -> None:
    ctx = _ctx()
    with _patch_add(), _patch_active(False):
        res = await _svc(ctx=ctx).add(UID, "Max")
    assert res.ok
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_remove_success_logs_with_actor() -> None:
    ctx = _ctx(added=[UID])
    with _patch_remove(), _patch_active():
        res = await _svc(ctx=ctx).remove(UID, "Max")
    assert res == AdminChange(True, "Removed Glob.")
    assert not ctx.admins.is_admin(UID)
    ctx.log_service.log_admin_removed.assert_awaited_once_with("Glob", UID, "Max")
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_remove_owner_and_non_admin() -> None:
    ctx = _ctx(owners=frozenset({UID}))
    svc = _svc(ctx=ctx)
    assert await svc.remove(UID, "Max") == AdminChange(
        False, "Owners can only be changed with ADMIN_USER_IDS."
    )
    assert await svc.remove(UID + 1, "Max") == AdminChange(
        False, "This person is not an added admin."
    )
    ctx.log_service.log_admin_removed.assert_not_awaited()


async def test_remove_save_failure_keeps_admin() -> None:
    ctx = _ctx(added=[UID])
    boom = AsyncMock(side_effect=RuntimeError("db"))
    with patch("bot.services.admin_registry.remove_web_admin", boom), _patch_active():
        res = await _svc(ctx=ctx).remove(UID, "Max")
    assert res == AdminChange(False, SAVE_FAILED)
    assert ctx.admins.is_admin(UID)
    ctx.log_service.log_admin_removed.assert_not_awaited()


async def test_remove_log_failure_still_succeeds_and_idle_skips() -> None:
    ctx = _ctx(added=[UID, UID + 1])
    ctx.log_service.log_admin_removed.side_effect = RuntimeError("x")
    with _patch_remove(), _patch_active():
        assert (await _svc(ctx=ctx).remove(UID, None)).ok
    ctx.log_service.log_admin_removed.side_effect = None
    ctx.log_service.log_admin_removed.reset_mock()
    with _patch_remove(), _patch_active(False):
        assert (await _svc(ctx=ctx).remove(UID + 1, None)).ok
    ctx.log_service.log_admin_removed.assert_not_awaited()


async def test_remove_name_falls_back_to_id_when_unknown() -> None:
    ctx = _ctx(added=[UID])
    with _patch_remove(), _patch_active():
        res = await _svc(bot=_bot(exc=_http(disnake.NotFound)), ctx=ctx).remove(UID, None)
    assert res.message == f"Removed {UID}."


async def test_list_admins_order_and_owner_flag() -> None:
    ctx = _ctx(owners=frozenset({30, 10}), added=[7, 5])
    rows = await _svc(ctx=ctx).list_admins()
    assert [(r[0], r[2]) for r in rows] == [(10, True), (30, True), (7, False), (5, False)]
    assert rows[0][1] == "Glob"


async def test_display_name_sources() -> None:
    member = SimpleNamespace(display_name="Nova")
    assert await _svc(bot=_bot(member=member)).display_name(UID) == "Nova"
    cached = SimpleNamespace(global_name="", name="cachedname")
    assert await _svc(bot=_bot(cached_user=cached)).display_name(UID) == "cachedname"
    assert await _svc(bot=_bot(_user())).display_name(UID) == "Glob"


@pytest.mark.parametrize("exc", [_http(disnake.NotFound), TimeoutError()])
async def test_display_name_unknown_is_none(exc) -> None:
    assert await _svc(bot=_bot(exc=exc)).display_name(UID) is None


def test_log_texts() -> None:
    assert texts.log_admin_added_text("Nova", 5) == "Nova (ID 5) was added as an admin."
    assert (
        texts.log_admin_added_text("Nova", 5, "Max")
        == "Nova (ID 5) was added as an admin by Max."
    )
    assert texts.LOG_ADMIN_REMOVED_TITLE == "Admin removed"
    assert texts.log_admin_removed_text("Nova", 5) == "Nova (ID 5) was removed as an admin."
    assert (
        texts.log_admin_removed_text("Nova", 5, "Max")
        == "Nova (ID 5) was removed as an admin by Max."
    )
