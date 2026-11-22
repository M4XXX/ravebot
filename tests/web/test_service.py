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
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=user, side_effect=user_exc)
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


def _svc(bot=None, ctx=None, clock=None):
    return AdminPageService(ctx or _ctx(), bot or _bot(_guild()), clock=clock or Clock())


def _patch_add(value=True):
    return patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=value))


def _patch_active(value=True):
    return patch("bot.services.admin_management.bot_is_active", AsyncMock(return_value=value))


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
    ctx.log_service.log_admin_added.assert_awaited_once_with("Glob", UID, "the admin page")


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
    with patch("bot.services.admin_registry.remove_web_admin", AsyncMock(return_value=True)), \
            _patch_active():
        res = await svc.remove_admin(str(UID + 1))
    assert (res.ok, res.message) == (True, "Removed Glob.")
    assert UID + 1 not in ctx.admins.added
    ctx.log_service.log_admin_added.assert_not_awaited()
    ctx.log_service.log_admin_removed.assert_awaited_once_with(
        "Glob", UID + 1, "the admin page"
    )


async def test_list_admins_owners_first() -> None:
    ctx = _ctx(owners=frozenset({30, 10}), added=[7])
    bot = _bot(_guild(), user=_user())
    ctx.admins.ensure_loaded = AsyncMock()
    rows = await _svc(bot=bot, ctx=ctx).list_admins()
    assert [(r.info.user_id, r.is_owner) for r in rows] == [(10, True), (30, True), (7, False)]
    ctx.admins.ensure_loaded.assert_awaited_once()


async def test_admin_row_owner_added_and_unknown() -> None:
    ctx = _ctx(owners=frozenset({UID}), added=[UID + 1])
    bot = _bot(_guild(), user=_user(uid=UID + 1))
    svc = _svc(bot=bot, ctx=ctx)
    owner = await svc.admin_row(str(UID))
    assert owner is not None and owner.is_owner and owner.info.user_id == UID
    added = await svc.admin_row(f" {UID + 1} ")
    assert added is not None and not added.is_owner and added.info.name == "Glob"
    assert await svc.admin_row(str(UID + 5)) is None
    for raw in ("12", "", "abc", str(2**63), "1" * 20):
        assert await svc.admin_row(raw) is None


async def test_admin_row_never_raises() -> None:
    ctx = _ctx(added=[UID])
    assert ctx.admins is not None
    ctx.admins.ensure_loaded = AsyncMock(side_effect=RuntimeError("db"))
    assert await _svc(ctx=ctx).admin_row(str(UID)) is None


async def test_recreate_notion_runs_the_recovery_once_needed() -> None:
    svc = _svc()
    ctx = svc._ctx
    ctx.notion_problem = None
    ctx.notion_recover = AsyncMock(return_value=None)
    result = await svc.recreate_notion()
    assert not result.ok
    ctx.notion_recover.assert_not_awaited()
    ctx.notion_problem = "gone"
    assert (await svc.recreate_notion()).ok
    ctx.notion_recover = AsyncMock(return_value="not shared")
    result = await svc.recreate_notion()
    assert not result.ok and "not shared" in result.message


# --- data and backup, Notion sync (Task 17) ---

import datetime  # noqa: E402

from bot import maintenance as maintenance_mod  # noqa: E402
from bot.db.outbox import OutboxStats  # noqa: E402
from bot.results import ActionResult  # noqa: E402
from bot.ui import texts  # noqa: E402


def _notion_svc():
    svc = _svc()
    ctx = svc._ctx
    ctx.notion_client = object()
    ctx.sync_cycle = MagicMock()
    ctx.sync_cycle.resolve_issue = AsyncMock(return_value=ActionResult(True, "kept"))
    ctx.sync_cycle.resync_all = AsyncMock(return_value=5)
    return svc, ctx


def _patch_status(last_run="2026-11-20T12:00:00+00:00", last_error=""):
    values = {"sync_last_run": last_run, "sync_last_error": last_error}
    return (
        patch("bot.web.service.config_db.get_value",
              AsyncMock(side_effect=lambda db, guild, key: values[key])),
        patch("bot.web.service.outbox.stats", AsyncMock(return_value=OutboxStats(2, None, None))),
        patch("bot.web.service.issues.list_open", AsyncMock(return_value=["issue"])),
    )


async def test_sync_status_disabled_without_notion() -> None:
    svc = _svc()
    a, b, c = _patch_status()
    with a as get_value, b as stats, c as list_open:
        status = await svc.sync_status()
    assert status.enabled is False and status.issues == []
    assert status.outbox == OutboxStats(0, None, None)
    get_value.assert_not_awaited()
    stats.assert_not_awaited()
    list_open.assert_not_awaited()


async def test_sync_status_can_retry_while_notion_not_connected() -> None:
    svc = _svc()
    ctx = svc._ctx
    assert (await svc.sync_status()).can_retry is False
    ctx.sync_problem = "The Notion databases are missing"
    assert (await svc.sync_status()).can_retry is False  # Notion not configured
    ctx.notion_connect = AsyncMock(return_value=None)
    status = await svc.sync_status()
    assert status.enabled is False and status.can_retry is True
    assert status.problem == "The Notion databases are missing"
    ctx.sync_problem = None
    assert (await svc.sync_status()).can_retry is False


async def test_sync_status_maps_empty_error_to_none() -> None:
    svc, ctx = _notion_svc()
    ctx.sync_problem = "Duplicate title"
    a, b, c = _patch_status()
    with a, b, c:
        status = await svc.sync_status()
    assert status.enabled and status.last_error is None
    assert status.last_run == datetime.datetime(2026, 11, 20, 12, tzinfo=datetime.UTC)
    assert status.outbox.count == 2 and status.issues == ["issue"]
    assert status.problem == "Duplicate title"
    a, b, c = _patch_status(last_run=None, last_error="boom")
    with a, b, c:
        status = await svc.sync_status()
    assert status.last_run is None and status.last_error == "boom"
    a, b, c = _patch_status(last_run="not a date")
    with a, b, c:
        assert (await svc.sync_status()).last_run is None


async def test_sync_status_shows_running_operation() -> None:
    svc = _svc()
    assert (await svc.sync_status()).maintenance is None
    claim = maintenance_mod.try_claim("rebuild")
    assert claim is not None
    with claim:
        text = (await svc.sync_status()).maintenance
    assert text is not None and "rebuild" in text.lower()
    svc._ctx.maintenance = texts.MAINTENANCE
    assert (await svc.sync_status()).maintenance is not None


async def test_resolve_issue_validates_and_delegates() -> None:
    svc, ctx = _notion_svc()
    for raw, choice in (("x", "notion"), ("7", "evil"), ("7", "expired"), ("-1", "notion")):
        assert not (await svc.resolve_issue(raw, choice)).ok
    ctx.sync_cycle.resolve_issue.assert_not_awaited()
    assert (await svc.resolve_issue("7", "notion")).ok
    ctx.sync_cycle.resolve_issue.assert_awaited_once_with(7, "notion")
    ctx.sync_cycle = None
    assert not (await svc.resolve_issue("7", "postgres")).ok


async def test_resync_all() -> None:
    svc, ctx = _notion_svc()
    result = await svc.resync_all()
    assert result.ok and "5" in result.message
    ctx.maintenance = texts.MAINTENANCE
    assert not (await svc.resync_all()).ok
    ctx.maintenance = None
    ctx.sync_cycle.resync_all = AsyncMock(return_value=0)
    assert not (await svc.resync_all()).ok
    ctx.sync_cycle = None
    assert not (await svc.resync_all()).ok


async def test_import_backup_rejects_non_gzip() -> None:
    svc = _svc()
    result = await svc.import_backup(b"not gzip at all")
    assert result == ActionResult(False, texts.IMPORT_UNREADABLE)
    assert maintenance_mod.running() is None


async def test_import_backup_never_raises() -> None:
    svc = _svc()
    with patch("bot.web.service.import_database", AsyncMock(side_effect=RuntimeError("x"))):
        result = await svc.import_backup(b"x")
    assert not result.ok


async def test_export_backup_reads_the_pool() -> None:
    svc = _svc()
    with patch("bot.web.service.export_database", AsyncMock(return_value=b"gz")) as export:
        assert await svc.export_backup() == b"gz"
    export.assert_awaited_once_with(svc._ctx.db)


async def test_rebuild_delegates_and_never_raises() -> None:
    svc, ctx = _notion_svc()
    with patch("bot.web.service.rebuild_from_notion",
               AsyncMock(return_value=ActionResult(True, "done"))) as rebuild:
        assert (await svc.rebuild_from_notion()).message == "done"
    rebuild.assert_awaited_once_with(ctx)
    with patch("bot.web.service.rebuild_from_notion", AsyncMock(side_effect=RuntimeError)):
        assert not (await svc.rebuild_from_notion()).ok


def _patch_retry(succeed: bool, ctx):
    async def upgrade(c):
        c.upgrade_import = "result"
        if succeed:
            c.maintenance = None
            c.sync_problem = None

    return (
        patch("bot.web.service.run_startup_rebuild_if_needed", AsyncMock(return_value=False)),
        patch("bot.web.service.run_upgrade_import_if_needed", AsyncMock(side_effect=upgrade)),
        patch("bot.web.service.post_upgrade_result", AsyncMock()),
        patch("bot.web.service.run_startup_recovery", AsyncMock()),
        patch("bot.web.service.post_rebuild_result", AsyncMock()),
    )


async def test_retry_import_success_starts_the_mirror() -> None:
    svc, ctx = _notion_svc()
    ctx.sync_cycle = None
    ctx.maintenance = texts.MAINTENANCE
    ctx.sync_problem = "Duplicate title"
    ctx.notion_start_mirror = AsyncMock()
    a, b, c, d, e = _patch_retry(True, ctx)
    with a, b as upgrade, c as post, d as recovery, e:
        result = await svc.retry_import()
    assert result.ok
    upgrade.assert_awaited_once_with(ctx)
    post.assert_awaited_once_with(ctx)
    ctx.notion_start_mirror.assert_awaited_once()
    recovery.assert_awaited_once_with(ctx)


async def test_retry_import_failure_keeps_the_pause() -> None:
    svc, ctx = _notion_svc()
    ctx.maintenance = texts.MAINTENANCE
    ctx.sync_problem = "Duplicate title"
    ctx.notion_start_mirror = AsyncMock()
    a, b, c, d, e = _patch_retry(False, ctx)
    with a, b, c as post, d as recovery, e:
        result = await svc.retry_import()
    assert not result.ok and "Duplicate title" in result.message
    post.assert_awaited_once_with(ctx)
    ctx.notion_start_mirror.assert_not_awaited()
    recovery.assert_not_awaited()


async def test_retry_import_refused_without_problem_or_while_busy() -> None:
    svc, ctx = _notion_svc()
    a, b, c, d, e = _patch_retry(True, ctx)
    with a, b as upgrade, c, d, e:
        assert not (await svc.retry_import()).ok
        ctx.sync_problem = "x"
        claim = maintenance_mod.try_claim("import")
        assert claim is not None
        with claim:
            result = await svc.retry_import()
        assert result == ActionResult(False, texts.OPERATION_BUSY)
        ctx.notion_client = None
        assert not (await svc.retry_import()).ok
    upgrade.assert_not_awaited()


async def test_retry_import_reconnects_notion_first() -> None:
    """C2: the databases were missing at startup; retry reconnects, then imports."""
    svc, ctx = _notion_svc()
    client = ctx.notion_client
    ctx.notion_client = None
    ctx.notion_problem = "Events database not found"
    ctx.maintenance = texts.MAINTENANCE
    ctx.sync_problem = texts.UPGRADE_WAITS_FOR_NOTION
    ctx.notion_start_mirror = AsyncMock()

    async def connect():
        ctx.notion_client = client
        ctx.notion_problem = None
        return None

    ctx.notion_connect = connect
    a, b, c, d, e = _patch_retry(True, ctx)
    with a, b as upgrade, c, d, e:
        result = await svc.retry_import()
    assert result.ok
    upgrade.assert_awaited_once_with(ctx)
    ctx.notion_start_mirror.assert_awaited_once()


async def test_retry_import_reconnect_fails_keeps_the_pause() -> None:
    svc, ctx = _notion_svc()
    ctx.notion_client = None
    ctx.notion_problem = "Events database not found"
    ctx.maintenance = texts.MAINTENANCE
    ctx.sync_problem = texts.UPGRADE_WAITS_FOR_NOTION
    ctx.notion_connect = AsyncMock(return_value="still not shared")
    a, b, c, d, e = _patch_retry(True, ctx)
    with a, b as upgrade, c, d, e:
        result = await svc.retry_import()
    assert not result.ok and "still not shared" in result.message
    upgrade.assert_not_awaited()
    assert ctx.maintenance == texts.MAINTENANCE


async def test_retry_import_reconnect_error_returns_result() -> None:
    svc, ctx = _notion_svc()
    ctx.notion_client = None
    ctx.maintenance = texts.MAINTENANCE
    ctx.sync_problem = texts.UPGRADE_WAITS_FOR_NOTION
    ctx.notion_connect = AsyncMock(side_effect=OSError("network down"))
    a, b, c, d, e = _patch_retry(True, ctx)
    with a, b as upgrade, c, d, e:
        result = await svc.retry_import()
    assert not result.ok
    upgrade.assert_not_awaited()
    assert ctx.maintenance == texts.MAINTENANCE


async def test_notion_import_pending_reads_the_mark() -> None:
    svc, _ctx = _notion_svc()
    values = {"pg_truth_imported": None}
    with patch("bot.web.service.config_db.get_value",
               AsyncMock(side_effect=lambda db, guild, key: values[key])):
        assert await svc.notion_import_pending() is True
        values["pg_truth_imported"] = "1"
        assert await svc.notion_import_pending() is False
    with patch("bot.web.service.config_db.get_value", AsyncMock(side_effect=OSError("x"))):
        assert await svc.notion_import_pending() is True


async def test_booking_rules_defaults_and_save() -> None:
    from bot.policy import DEFAULT_RULES, BookingRules

    store: dict[str, str] = {}

    async def get_value(db, guild_id, key):
        assert guild_id == 1
        return store.get(key)

    async def set_value(db, guild_id, key, value):
        assert guild_id == 1
        store[key] = value

    svc = _svc()
    with (
        patch("bot.db.config.get_value", get_value),
        patch("bot.db.config.set_value", set_value),
    ):
        assert await svc.booking_rules() == DEFAULT_RULES
        bad = await svc.set_booking_rules({"slot_limit_max": "x", "cancel_cutoff_minutes": "5"})
        assert not bad.ok and "whole number" in bad.message and store == {}
        ok = await svc.set_booking_rules(
            {"slot_limit_enabled": "on", "slot_limit_max": "3", "cancel_cutoff_minutes": "0"}
        )
        assert ok.ok and ok.message == "Booking rules saved."
        assert store == {
            "slot_limit_enabled": "true", "slot_limit_max": "3",
            "cancel_cutoff_enabled": "false", "cancel_cutoff_minutes": "0",
        }
        assert await svc.booking_rules() == BookingRules(True, 3, False, 0)


async def test_booking_rules_save_without_guild_fails() -> None:
    result = await _svc(bot=_bot()).set_booking_rules(
        {"slot_limit_max": "3", "cancel_cutoff_minutes": "5"}
    )
    assert not result.ok


async def test_booking_rules_save_failure_returns_fixed_message() -> None:
    async def boom(db, guild_id, key, value):
        raise RuntimeError("secret detail")

    with patch("bot.db.config.set_value", boom):
        result = await _svc().set_booking_rules(
            {"slot_limit_max": "3", "cancel_cutoff_minutes": "5"}
        )
    assert not result.ok
    assert result.message == "The booking rules could not be saved. Please try again."
