"""Tests for bot/web/service.py."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.context import BotContext
from bot.services.admin_registry import AdminRegistry
from bot.web.service import AdminPageService, UserInfo

UID = 123456789012345678


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _avatar(url="http://a/x.png"):
    av = MagicMock()
    av.with_size.return_value.url = url
    return av


def _member(uid=UID, name="Nova"):
    return SimpleNamespace(id=uid, display_name=name, display_avatar=_avatar())


def _user(uid=UID, global_name="Glob", name="raw"):
    return SimpleNamespace(
        id=uid, global_name=global_name, name=name, display_avatar=_avatar("http://a/u.png")
    )


def _http(cls=disnake.HTTPException):
    resp = MagicMock()
    resp.status = 404
    return cls(resp, "x")


def _guild(member=None, fetched=None):
    g = MagicMock()
    g.id = 1
    g.name = "Rave"
    g.icon = SimpleNamespace(url="http://i/g.png")
    g.get_member = MagicMock(return_value=member)
    if fetched is None:
        g.fetch_member = AsyncMock(side_effect=_http(disnake.NotFound))
    else:
        g.fetch_member = AsyncMock(return_value=fetched)
    return g


def _bot(guild=None, user=None, user_exc=None):
    bot = MagicMock()
    bot.guilds = [guild] if guild is not None else []
    bot.fetch_user = AsyncMock(return_value=user, side_effect=user_exc)
    return bot


def _ctx(owners=frozenset({10}), added=()):
    reg = AdminRegistry(owners, object())
    reg._added = list(added)
    reg._loaded = True
    ctx = BotContext(config=MagicMock(), db=object(), admins=reg)
    ctx.log_service = MagicMock()
    ctx.log_service.log_admin_added = AsyncMock()
    return ctx


def _svc(bot=None, ctx=None, clock=None):
    return AdminPageService(ctx or _ctx(), bot or _bot(_guild()), clock=clock or Clock())


def _patch_add(value=True):
    return patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=value))


def _patch_active(value=True):
    return patch("bot.web.service.bot_is_active", AsyncMock(return_value=value))


def test_requires_registry() -> None:
    ctx = BotContext(config=MagicMock(), db=object())
    with pytest.raises(RuntimeError):
        AdminPageService(ctx, _bot())


def test_server_info() -> None:
    info = _svc().server_info()
    assert (info.guild_id, info.name, info.icon_url) == (1, "Rave", "http://i/g.png")
    assert _svc(bot=_bot()).server_info() is None


async def test_describe_member_cache() -> None:
    bot = _bot(_guild(member=_member()))
    info = await _svc(bot=bot).describe(UID)
    assert info == UserInfo(UID, "Nova", "http://a/x.png", True)
    bot.fetch_user.assert_not_awaited()


async def test_describe_fetch_member_fallback() -> None:
    g = _guild(member=None, fetched=_member(name="Fetched"))
    info = await _svc(bot=_bot(g)).describe(UID)
    assert (info.name, info.on_server) == ("Fetched", True)


async def test_describe_fetch_user_fallback() -> None:
    bot = _bot(_guild(), user=_user())
    info = await _svc(bot=bot).describe(UID)
    assert info == UserInfo(UID, "Glob", "http://a/u.png", False)
    bot.fetch_user.assert_awaited_once_with(UID)


async def test_describe_uses_username_without_global_name() -> None:
    bot = _bot(_guild(), user=_user(global_name=None))
    assert (await _svc(bot=bot).describe(UID)).name == "raw"


async def test_describe_total_failure_never_raises() -> None:
    g = _guild()
    g.get_member.side_effect = RuntimeError("boom")
    bot = _bot(g, user_exc=RuntimeError("down"))
    info = await _svc(bot=bot).describe(UID)
    assert info == UserInfo(UID, None, None, False)


async def test_describe_without_guild_uses_user() -> None:
    bot = _bot(None, user=_user())
    assert (await _svc(bot=bot).describe(UID)).on_server is False


async def test_describe_caching_and_expiry() -> None:
    clock = Clock()
    bot = _bot(_guild(), user=_user())
    svc = _svc(bot=bot, clock=clock)
    await svc.describe(UID)
    await svc.describe(UID)
    assert bot.fetch_user.await_count == 1
    clock.now += 299
    await svc.describe(UID)
    assert bot.fetch_user.await_count == 1
    clock.now += 2
    await svc.describe(UID)
    assert bot.fetch_user.await_count == 2


async def test_describe_failure_cached_30_seconds() -> None:
    clock = Clock()
    bot = _bot(_guild(), user_exc=RuntimeError("down"))
    svc = _svc(bot=bot, clock=clock)
    await svc.describe(UID)
    await svc.describe(UID)
    assert bot.fetch_user.await_count == 1
    clock.now += 31
    await svc.describe(UID)
    assert bot.fetch_user.await_count == 2


async def test_describe_sanitises_name() -> None:
    long = "a\x00b\n" + "x" * 200
    bot = _bot(_guild(member=_member(name=long)))
    name = (await _svc(bot=bot).describe(UID)).name
    assert len(name) == 80
    assert name.startswith("ab")
    assert "\x00" not in name and "\n" not in name


@pytest.mark.parametrize("raw", ["1" * 17, str(2**63 - 1), f"  {'2' * 18}  "])
async def test_add_admin_accepts_valid_ids(raw) -> None:
    bot = _bot(_guild(), user=_user(uid=int(raw.strip())))
    ctx = _ctx()
    with _patch_add(), _patch_active():
        res = await _svc(bot=bot, ctx=ctx).add_admin(raw)
    assert res.ok
    assert int(raw.strip()) in ctx.admins.added


FORMAT_MSG = "Please enter a Discord user ID (17 to 20 digits)."


@pytest.mark.parametrize(
    "raw",
    [
        "1" * 16, "1" * 20, "1" * 21, "-" + "1" * 18, "123 456 789 012 345 678",
        "abc" * 6, "", "  ", "１" * 18,
    ],
)
async def test_add_admin_rejects_bad_format(raw) -> None:
    bot = _bot(_guild(), user=_user())
    res = await _svc(bot=bot).add_admin(raw)
    assert (res.ok, res.message) == (False, FORMAT_MSG)
    bot.fetch_user.assert_not_awaited()


async def test_add_admin_owner_and_duplicate_untouched() -> None:
    bot = _bot(_guild(), user=_user())
    ctx = _ctx(owners=frozenset({UID}), added=[UID + 1])
    svc = _svc(bot=bot, ctx=ctx)
    with _patch_add() as add:
        for uid in (UID, UID + 1):
            res = await svc.add_admin(str(uid))
            assert (res.ok, res.message) == (False, "This person is already an admin.")
    bot.fetch_user.assert_not_awaited()
    add.assert_not_awaited()


async def test_add_admin_not_found() -> None:
    bot = _bot(_guild(), user_exc=_http(disnake.NotFound))
    with _patch_add() as add:
        res = await _svc(bot=bot).add_admin(str(UID))
    assert (res.ok, res.message) == (False, "No Discord user with this ID.")
    add.assert_not_awaited()


async def test_add_admin_http_error() -> None:
    bot = _bot(_guild(), user_exc=_http())
    with _patch_add() as add:
        res = await _svc(bot=bot).add_admin(str(UID))
    assert (res.ok, res.message) == (False, "Could not reach Discord. Try again.")
    add.assert_not_awaited()


async def test_add_admin_success_logs_once() -> None:
    bot = _bot(_guild(), user=_user())
    ctx = _ctx()
    with _patch_add(), _patch_active():
        res = await _svc(bot=bot, ctx=ctx).add_admin(str(UID))
    assert (res.ok, res.message) == (True, "Added Glob.")
    assert UID in ctx.admins.added
    ctx.log_service.log_admin_added.assert_awaited_once_with("Glob", UID)


async def test_add_admin_name_falls_back_to_id() -> None:
    bot = _bot(_guild(), user=_user(global_name="", name=""))
    with _patch_add(), _patch_active():
        res = await _svc(bot=bot).add_admin(str(UID))
    assert res.message == f"Added {UID}."


async def test_add_admin_log_failure_does_not_change_result() -> None:
    bot = _bot(_guild(), user=_user())
    ctx = _ctx()
    ctx.log_service.log_admin_added.side_effect = RuntimeError("x")
    with _patch_add(), _patch_active():
        res = await _svc(bot=bot, ctx=ctx).add_admin(str(UID))
    assert res.ok


async def test_add_admin_idle_bot_no_log() -> None:
    bot = _bot(_guild(), user=_user())
    ctx = _ctx()
    with _patch_add(), _patch_active(False):
        res = await _svc(bot=bot, ctx=ctx).add_admin(str(UID))
    assert res.ok
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_add_admin_registry_refuses() -> None:
    bot = _bot(_guild(), user=_user())
    ctx = _ctx()
    with _patch_add(False), _patch_active():
        res = await _svc(bot=bot, ctx=ctx).add_admin(str(UID))
    assert (res.ok, res.message) == (False, "This person is already an admin.")
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_add_admin_db_error() -> None:
    bot = _bot(_guild(), user=_user())
    ctx = _ctx()
    boom = AsyncMock(side_effect=RuntimeError("db"))
    with patch("bot.services.admin_registry.add_web_admin", boom), _patch_active():
        res = await _svc(bot=bot, ctx=ctx).add_admin(str(UID))
    assert (res.ok, res.message) == (False, "Could not save the change. Try again.")
    assert UID not in ctx.admins.added
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_remove_admin_db_error() -> None:
    ctx = _ctx(added=[UID])
    bot = _bot(_guild(), user=_user())
    boom = AsyncMock(side_effect=RuntimeError("db"))
    with patch("bot.services.admin_registry.remove_web_admin", boom):
        res = await _svc(bot=bot, ctx=ctx).remove_admin(str(UID))
    assert (res.ok, res.message) == (False, "Could not save the change. Try again.")


@pytest.mark.parametrize("exc", [TimeoutError(), RuntimeError("x")])
async def test_add_admin_other_lookup_errors(exc) -> None:
    bot = _bot(_guild(), user_exc=exc)
    ctx = _ctx()
    with _patch_add() as add, _patch_active():
        res = await _svc(bot=bot, ctx=ctx).add_admin(str(UID))
    assert (res.ok, res.message) == (False, "Could not reach Discord. Try again.")
    add.assert_not_awaited()
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_id_int64_boundary() -> None:
    assert len(str(2**63)) == 19
    res = await _svc().add_admin(str(2**63))
    assert (res.ok, res.message) == (False, FORMAT_MSG)
    res = await _svc().remove_admin(str(2**63))
    assert (res.ok, res.message) == (False, FORMAT_MSG)
    bot = _bot(_guild(), user=_user(uid=2**63 - 1))
    with _patch_add(), _patch_active():
        assert (await _svc(bot=bot).add_admin(str(2**63 - 1))).ok


async def test_add_admin_ensures_loaded() -> None:
    ctx = _ctx()
    ctx.admins.ensure_loaded = AsyncMock()
    await _svc(ctx=ctx).add_admin("x")
    ctx.admins.ensure_loaded.assert_awaited_once()


async def test_remove_admin_cases() -> None:
    ctx = _ctx(owners=frozenset({UID}), added=[UID + 1])
    bot = _bot(_guild(), user=_user(uid=UID + 1))
    svc = _svc(bot=bot, ctx=ctx)
    assert (await svc.remove_admin("12")).message == FORMAT_MSG
    res = await svc.remove_admin(str(UID))
    assert (res.ok, res.message) == (False, "Owners can only be changed with ADMIN_USER_IDS.")
    res = await svc.remove_admin(str(UID + 5))
    assert (res.ok, res.message) == (False, "This person is not an added admin.")
    with patch("bot.services.admin_registry.remove_web_admin", AsyncMock(return_value=True)):
        res = await svc.remove_admin(str(UID + 1))
    assert (res.ok, res.message) == (True, "Removed Glob.")
    assert UID + 1 not in ctx.admins.added
    ctx.log_service.log_admin_added.assert_not_awaited()


async def test_list_admins_owners_first() -> None:
    ctx = _ctx(owners=frozenset({30, 10}), added=[7])
    bot = _bot(_guild(), user=_user())
    ctx.admins.ensure_loaded = AsyncMock()
    rows = await _svc(bot=bot, ctx=ctx).list_admins()
    assert [(r.info.user_id, r.is_owner) for r in rows] == [(10, True), (30, True), (7, False)]
    ctx.admins.ensure_loaded.assert_awaited_once()
