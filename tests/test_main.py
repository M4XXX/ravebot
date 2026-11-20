"""Tests for bot/main.py — bot factory and startup wiring."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.main import create_bot
from bot.notion.api import NotionWriteError
from bot.notion.schema import NotionIds, NotionSchemaError


class TestCreateBot:
    def test_members_intent_enabled(self) -> None:
        """members intent must be True — required for on_member_update (FR1 DJ role detection)."""
        bot = create_bot()
        assert bot.intents.members is True

    def test_default_intents_enabled(self) -> None:
        """Default intents are present alongside the members intent."""
        bot = create_bot()
        assert bot.intents.guilds is True


_IDS = NotionIds(
    events_ds="ev-ds",
    slots_ds="sl-ds",
    djs_ds="dj-ds",
    events_db="ev-db",
    slots_db="sl-db",
    djs_db="dj-db",
    slots_event_property_id="prop",
)


class _Startup:
    """Patches every external dependency of main() and records the call order."""

    def __init__(
        self, schema_error: Exception | None = None, notion_enabled: bool = True
    ) -> None:
        self.calls: list[str] = []
        self.config = MagicMock()
        self.config.log_level = "INFO"
        self.config.notion_enabled = notion_enabled
        self.config.notion_api_key = "key"
        self.config.notion_parent_page_id = "parent"
        self.config.discord_bot_token = "token"
        self.pool = MagicMock()
        self.pool.close = AsyncMock()
        self.bot = MagicMock()
        self.bot.start = AsyncMock(side_effect=self._start)
        self.api = MagicMock()
        self.api.close = AsyncMock()
        self.client = MagicMock()
        self.ctx = None
        self.runner = object()
        self.start_page = AsyncMock(side_effect=self._start_page)
        self.stop_page = AsyncMock(side_effect=self._stop_page)
        self._schema_error = schema_error
        self.while_running = None  # async callback run while main() is running
        self.import_fails = False  # the upgrade import leaves the bot in maintenance
        self.queued_with_mirror: list[object] = []  # ctx.mirror at each logo-queue call
        self.recovery_mirrors: list[object] = []  # ctx.mirror at each startup-case check
        self.rebuild_instead = False  # the startup rebuild replaced the upgrade import

    async def _queue_logos(self, ctx):  # noqa: ANN001, ANN202
        self.calls.append("queue_logos")
        self.queued_with_mirror.append(ctx.mirror)
        return 0

    async def _upgrade_import(self, ctx):  # noqa: ANN001, ANN202
        self.calls.append("upgrade_import")
        if self.import_fails:
            ctx.maintenance = "restoring"

    async def _startup_rebuild(self, ctx):  # noqa: ANN001, ANN202
        self.calls.append("startup_rebuild")
        return self.rebuild_instead

    async def _startup_recovery(self, ctx):  # noqa: ANN001, ANN202
        self.calls.append("startup_recovery")
        self.recovery_mirrors.append(ctx.mirror)

    async def _start(self, token: str) -> None:
        self.calls.append("start")
        if self.while_running is not None:
            await self.while_running()

    async def _start_page(self, ctx, bot):  # noqa: ANN001, ANN202
        self.calls.append("page_start")
        return self.runner

    async def _stop_page(self, runner):  # noqa: ANN001, ANN202
        self.calls.append("page_stop")

    async def _ensure_schema(self, api, db, guild_id, parent):  # noqa: ANN001, ANN202
        self.calls.append("ensure_schema")
        assert (api, db, guild_id, parent) == (self.api, self.pool, 0, "parent")
        if self._schema_error is not None:
            raise self._schema_error
        return _IDS

    async def _ensure_views(self, api, db, guild_id, ids):  # noqa: ANN001, ANN202
        self.calls.append("ensure_views")
        assert (api, db, guild_id, ids) == (self.api, self.pool, 0, _IDS)

    def _make_client(self, api, ids):  # noqa: ANN001, ANN202
        self.calls.append("client")
        assert (api, ids) == (self.api, _IDS)
        return self.client

    def _capture_ctx(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        from bot.context import BotContext

        ctx = BotContext(*args, **kwargs)
        self.ctx = ctx
        return ctx

    async def run(self) -> None:
        from bot import main as main_mod

        with patch.object(main_mod.Config, "from_env", return_value=self.config), \
             patch.object(main_mod, "_setup_logging"), \
             patch.object(main_mod, "run_migrations", new=AsyncMock()), \
             patch.object(main_mod, "create_pool", new=AsyncMock(return_value=self.pool)), \
             patch.object(main_mod, "create_bot", return_value=self.bot), \
             patch.object(main_mod, "BotContext", side_effect=self._capture_ctx), \
             patch.object(main_mod, "NotionApi", return_value=self.api) as api_cls, \
             patch.object(main_mod, "ensure_schema", side_effect=self._ensure_schema), \
             patch.object(main_mod, "ensure_views", side_effect=self._ensure_views), \
             patch.object(main_mod, "NotionClient", side_effect=self._make_client), \
             patch.object(main_mod, "start_admin_page", new=self.start_page), \
             patch.object(
                 main_mod, "run_upgrade_import_if_needed", side_effect=self._upgrade_import
             ), \
             patch.object(main_mod, "queue_unsynced_logos", side_effect=self._queue_logos), \
             patch.object(
                 main_mod, "run_startup_recovery", side_effect=self._startup_recovery
             ), \
             patch.object(
                 main_mod, "run_startup_rebuild_if_needed", side_effect=self._startup_rebuild
             ), \
             patch.object(main_mod, "stop_admin_page", new=self.stop_page):
            self.api_cls = api_cls
            await main_mod.main()


@pytest.mark.asyncio
async def test_startup_ensures_schema_then_views_for_guild_zero() -> None:
    s = _Startup()
    await s.run()

    assert s.calls == [
        "ensure_schema", "ensure_views", "client", "startup_rebuild", "upgrade_import",
        "queue_logos", "startup_recovery", "page_start", "start", "page_stop",
    ]
    assert s.api_cls.call_args.args[0] == "key"


@pytest.mark.asyncio
async def test_startup_builds_services_with_new_client() -> None:
    s = _Startup()
    await s.run()

    ctx = s.ctx
    assert ctx is not None
    assert ctx.notion_client is s.client
    assert not hasattr(ctx, "notion_sync_service")
    # Booking, lineup, outreach and reminders work on Postgres alone
    for service in (
        ctx.outreach_service, ctx.reminders_service, ctx.admin_booking_service,
        ctx.lineup_service,
    ):
        assert not hasattr(service, "notion_client")
    assert ctx.admin_booking_service._ctx is ctx


@pytest.mark.asyncio
async def test_startup_registers_log_reply_view() -> None:
    from bot.ui.log_reply import LogReplyView

    s = _Startup()
    await s.run()
    added = [c.args[0] for c in s.bot.add_view.call_args_list]
    assert any(isinstance(v, LogReplyView) for v in added)


@pytest.mark.asyncio
async def test_startup_registers_lineup_pick_view() -> None:
    from bot.ui.views import LineupEventPickView

    s = _Startup()
    await s.run()
    added = [c.args[0] for c in s.bot.add_view.call_args_list]
    assert any(isinstance(v, LineupEventPickView) for v in added)


@pytest.mark.asyncio
async def test_startup_registers_setup_wizard_view_and_cog() -> None:
    from bot.cogs.setup import SetupCog
    from bot.ui.setup_wizard import SetupWizardView

    s = _Startup()
    await s.run()
    added = [c.args[0] for c in s.bot.add_view.call_args_list]
    assert any(isinstance(v, SetupWizardView) for v in added)
    cogs = [c.args[0] for c in s.bot.add_cog.call_args_list]
    assert any(isinstance(c, SetupCog) and c.bot is s.bot for c in cogs)


@pytest.mark.asyncio
async def test_on_ready_only_logs_and_touches_no_notion() -> None:
    """The schema is ensured before Discord connects; on_ready starts no Notion task."""
    s = _Startup()
    await s.run()

    s.bot.event.assert_called_once()
    on_ready = s.bot.event.call_args.args[0]
    assert on_ready.__name__ == "on_ready"
    await on_ready()
    s.bot.loop.create_task.assert_not_called()
    assert s.client.method_calls == []


@pytest.mark.asyncio
async def test_shutdown_closes_pool_and_notion_api() -> None:
    s = _Startup()
    await s.run()

    s.pool.close.assert_awaited_once()
    s.api.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [NotionSchemaError("bad schema"), NotionWriteError("404", status_code=404)],
)
async def test_startup_fails_fast_when_notion_setup_fails(error: Exception) -> None:
    s = _Startup(schema_error=error)

    with pytest.raises(SystemExit) as exc_info:
        await s.run()

    message = str(exc_info.value.code)
    assert "Cannot use the Notion parent page" in message
    assert "Connections" in message
    assert "NOTION_API_KEY" in message
    assert "NOTION_PARENT_PAGE_ID" in message
    assert "start" not in s.calls
    s.pool.close.assert_awaited_once()
    s.api.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_creates_dj_conversation_service_with_the_bot() -> None:
    from bot.services.dj_conversation import DjConversationService

    s = _Startup()
    await s.run()
    service = s.ctx.dj_conversations
    assert isinstance(service, DjConversationService)
    assert service._client is s.bot
    assert service._ctx is s.ctx


@pytest.mark.asyncio
async def test_startup_registers_dj_profile_and_menu_views() -> None:
    from bot.ui.gates import GatedView

    s = _Startup()
    await s.run()
    added = [c.args[0] for c in s.bot.add_view.call_args_list]
    found = [
        c.custom_id for v in added if isinstance(v, GatedView) for c in v.children
    ]
    expected = [
        "onboarding:go_active", "onboarding:pass_for_now",
        "dj:skip", "dj:use_as_is", "dj:remove", "dj:cancel", "dj:confirm", "dj:start_over",
        "dj:edit:name", "dj:edit:vrchat", "dj:edit:logo", "dj:edit:link", "dj:edit:alt_link",
        "dj:leave", "dj:join",
        "fallback:book_slot", "fallback:cancel_slot", "fallback:go_active",
        "fallback:go_inactive", "fallback:show_lineup", "fallback:my_profile",
    ]
    for custom_id in expected:
        assert found.count(custom_id) == 1, custom_id
    for view in added:
        if isinstance(view, GatedView):
            assert view.ctx is s.ctx


async def _ready(s: "_Startup", active: bool, times: int) -> None:
    from bot import main as main_mod

    s.bot.guilds = [MagicMock()]
    with patch.object(main_mod, "load_stored_timezone", new=AsyncMock()), \
         patch.object(main_mod, "notify_admins_if_idle", new=AsyncMock()), \
         patch.object(main_mod.activation, "bot_is_active", new=AsyncMock(return_value=active)):
        on_ready = s.bot.event.call_args.args[0]
        for _ in range(times):
            await on_ready()


@pytest.mark.asyncio
async def test_unsynced_logos_are_queued_once_the_mirror_runs() -> None:
    """The worker uploads unsynced logos: startup queues those DJs after it starts."""
    from bot.sync.worker import MirrorWorker

    s = _Startup()
    await s.run()
    assert len(s.queued_with_mirror) == 1
    assert isinstance(s.queued_with_mirror[0], MirrorWorker)
    assert s.calls.index("upgrade_import") < s.calls.index("queue_logos") < s.calls.index("start")
    await _ready(s, active=True, times=2)  # on_ready no longer retries logos itself
    assert len(s.queued_with_mirror) == 1


@pytest.mark.asyncio
async def test_unsynced_logos_not_queued_without_the_mirror() -> None:
    for s in (_Startup(notion_enabled=False), _Startup()):
        s.import_fails = True
        await s.run()
        assert "queue_logos" not in s.calls


def test_main_no_longer_retries_logos_inline() -> None:
    from bot import main as main_mod

    assert not hasattr(main_mod, "_retry_logos_if_active")
    assert not hasattr(main_mod, "retry_unsynced_logos")


@pytest.mark.asyncio
async def test_on_ready_retries_the_admin_registry_load_once_per_process() -> None:
    s = _Startup()
    await s.run()
    s.ctx.admins = MagicMock()
    s.ctx.admins.ensure_loaded = AsyncMock()
    await _ready(s, active=True, times=2)
    s.ctx.admins.ensure_loaded.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_ready_survives_a_failing_admin_registry_load() -> None:
    s = _Startup()
    await s.run()
    s.ctx.admins = MagicMock()
    s.ctx.admins.ensure_loaded = AsyncMock(side_effect=RuntimeError("db"))
    await _ready(s, active=True, times=1)
    s.ctx.admins.ensure_loaded.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_creates_and_loads_the_admin_registry() -> None:
    from bot.services.admin_registry import AdminRegistry

    s = _Startup()
    s.config.admin_user_ids = frozenset({30, 10})
    with patch("bot.services.admin_registry.list_web_admins", AsyncMock(return_value=[7])) as lst:
        await s.run()
    lst.assert_awaited_once_with(s.pool)
    assert isinstance(s.ctx.admins, AdminRegistry)
    assert s.ctx.is_admin(7) and s.ctx.is_admin(10) and not s.ctx.is_admin(99)


@pytest.mark.asyncio
async def test_startup_dm_goes_to_owners_plus_added_admins() -> None:
    from bot import main as main_mod

    s = _Startup()
    s.config.admin_user_ids = frozenset({30, 10})
    with patch("bot.services.admin_registry.list_web_admins", AsyncMock(return_value=[7])):
        await s.run()
    s.bot.guilds = []
    notify = AsyncMock()
    with patch.object(main_mod, "notify_admins_if_idle", new=notify), \
         patch.object(main_mod.activation, "bot_is_active", new=AsyncMock(return_value=False)):
        await s.bot.event.call_args.args[0]()
    notify.assert_awaited_once_with(s.bot, s.ctx.db, [10, 30, 7])


@pytest.mark.asyncio
async def test_admin_page_starts_before_the_bot_and_stops_first_in_finally() -> None:
    s = _Startup()
    await s.run()
    s.start_page.assert_awaited_once_with(s.ctx, s.bot)
    s.stop_page.assert_awaited_once_with(s.runner)
    assert s.calls.index("page_start") < s.calls.index("start") < s.calls.index("page_stop")


@pytest.mark.asyncio
async def test_admin_page_stops_even_when_the_bot_fails() -> None:
    s = _Startup()
    s.bot.start = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await s.run()
    s.stop_page.assert_awaited_once_with(s.runner)
    s.pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_databases_keep_the_bot_running_until_an_admin_decides() -> None:
    from bot.notion.schema import NotionDatabasesMissing

    s = _Startup(schema_error=NotionDatabasesMissing("DJs data source x returned 404"))
    stored: dict = {}
    seen: dict = {}
    seen_mirror: list = []

    async def put(db, guild_id, key, value):  # noqa: ANN001, ANN202
        stored[key] = value

    async def admin_approves() -> None:
        ctx = s.ctx
        seen["client"] = ctx.notion_client
        seen["problem"] = ctx.notion_problem
        seen_mirror.append(ctx.mirror)
        s._schema_error = None
        seen["error"] = await ctx.notion_recover()

    s.while_running = admin_approves
    with patch("bot.main.set_value", new=put):
        await s.run()
    ctx = s.ctx
    assert seen == {"client": None, "problem": "DJs data source x returned 404", "error": None}
    assert stored["notion_recreate_approved"] == "1"
    assert ctx.notion_problem is None
    assert ctx.notion_client is s.client
    assert seen_mirror == [None]
    assert ctx.mirror is not None and ctx.mirror.notion_client is s.client


@pytest.mark.asyncio
async def test_failed_recovery_withdraws_the_approval() -> None:
    from bot.notion.schema import NotionDatabasesMissing

    s = _Startup(schema_error=NotionDatabasesMissing("gone"))
    stored: dict = {}
    seen: dict = {}

    async def put(db, guild_id, key, value):  # noqa: ANN001, ANN202
        stored[key] = value

    async def admin_approves() -> None:
        s._schema_error = NotionWriteError("parent page not shared", status_code=404)
        seen["error"] = await s.ctx.notion_recover()

    s.while_running = admin_approves
    with patch("bot.main.set_value", new=put):
        await s.run()
    assert "parent page not shared" in seen["error"]
    assert stored["notion_recreate_approved"] == "0"
    assert s.ctx.notion_problem == "gone"


@pytest.mark.asyncio
async def test_startup_builds_the_event_cancel_service() -> None:
    from bot.services.event_cancel import EventCancelService

    s = _Startup()
    await s.run()

    assert s.ctx is not None
    assert isinstance(s.ctx.event_cancel_service, EventCancelService)


@pytest.mark.asyncio
async def test_main_without_notion_skips_setup() -> None:
    s = _Startup(notion_enabled=False)
    await s.run()

    assert "ensure_schema" not in s.calls
    assert "ensure_views" not in s.calls
    assert "client" not in s.calls
    s.api_cls.assert_not_called()
    ctx = s.ctx
    assert ctx.notion_client is None
    assert ctx.notion_recover is None
    assert ctx.notion_problem is None
    assert ctx.mirror is None
    assert ctx.admin_booking_service is not None
    s.pool.close.assert_awaited_once()
    on_ready = s.bot.event.call_args.args[0]
    await on_ready()


@pytest.mark.asyncio
async def test_mirror_worker_runs_while_the_bot_runs() -> None:
    from bot.sync.worker import MirrorWorker

    s = _Startup()
    seen: dict = {}

    async def check() -> None:
        mirror = s.ctx.mirror
        seen["running"] = isinstance(mirror, MirrorWorker) and not mirror._task.done()
        seen["client"] = mirror.notion_client
        seen["mirror"] = mirror
        seen["name"] = mirror._name_resolver

    s.while_running = check
    await s.run()
    assert seen["running"] is True
    assert seen["client"] is s.client
    assert seen["mirror"]._task is None  # stopped on shutdown
    # The "Discord name" column comes from the bot's user cache
    s.bot.get_user = MagicMock(return_value=MagicMock(display_name="alice"))
    assert seen["name"](42) == "alice"
    s.bot.get_user.assert_called_once_with(42)
    s.bot.get_user = MagicMock(return_value=None)
    assert seen["name"](43) is None


@pytest.mark.asyncio
async def test_upgrade_import_runs_before_discord_and_the_mirror() -> None:
    s = _Startup()
    seen: dict = {}

    async def check() -> None:
        seen["mirror"] = s.ctx.mirror

    s.while_running = check
    await s.run()
    assert s.calls.index("client") < s.calls.index("upgrade_import") < s.calls.index("start")
    assert seen["mirror"] is not None


@pytest.mark.asyncio
async def test_failed_upgrade_import_keeps_the_mirror_off() -> None:
    s = _Startup()
    s.import_fails = True
    seen: dict = {}

    async def check() -> None:
        seen["mirror"] = s.ctx.mirror

    s.while_running = check
    await s.run()
    assert seen["mirror"] is None
    assert s.ctx.maintenance == "restoring"


@pytest.mark.asyncio
async def test_main_without_notion_does_not_enter_maintenance() -> None:
    s = _Startup(notion_enabled=False)
    await s.run()
    assert "upgrade_import" not in s.calls
    assert s.ctx.maintenance is None


@pytest.mark.asyncio
async def test_on_ready_posts_the_upgrade_result() -> None:
    from bot.sync.notion_import import ImportResult

    s = _Startup()
    await s.run()
    s.ctx.upgrade_import = ImportResult(ok=True, events=1, slots=2, djs=3, problems=[])
    s.ctx.log_service = MagicMock()
    s.ctx.log_service.log_notice = AsyncMock()
    on_ready = s.bot.event.call_args.args[0]
    with patch("bot.main.notify_admins_if_idle", new=AsyncMock()):
        await on_ready()
        await on_ready()
    s.ctx.log_service.log_notice.assert_awaited_once()
    assert s.ctx.upgrade_import is None


@pytest.mark.asyncio
async def test_startup_cases_run_after_the_upgrade_import_and_the_mirror() -> None:
    s = _Startup()
    await s.run()
    assert s.calls.index("upgrade_import") < s.calls.index("startup_recovery")
    assert s.calls.index("startup_recovery") < s.calls.index("start")
    assert s.recovery_mirrors[0] is not None  # resync_all needs the running mirror


@pytest.mark.asyncio
async def test_startup_cases_skipped_without_notion() -> None:
    s = _Startup(notion_enabled=False)
    await s.run()
    assert "startup_recovery" not in s.calls


@pytest.mark.asyncio
async def test_recovered_notion_runs_the_startup_cases() -> None:
    from bot.notion.schema import NotionDatabasesMissing

    s = _Startup(schema_error=NotionDatabasesMissing("gone"))

    async def admin_approves() -> None:
        s._schema_error = None
        assert s.calls.count("startup_recovery") == 1  # startup (no client yet)
        assert await s.ctx.notion_recover() is None

    s.while_running = admin_approves
    with patch("bot.main.set_value", new=AsyncMock()):
        await s.run()
    assert s.calls.count("startup_recovery") == 2
    assert s.recovery_mirrors[-1] is not None


@pytest.mark.asyncio
async def test_on_ready_posts_the_rebuild_result() -> None:
    from bot.sync.notion_import import ImportResult
    from bot.sync.rebuild import RebuildReport

    s = _Startup()
    await s.run()
    s.ctx.rebuild_report = RebuildReport(ImportResult(True, 1, 2, 3, []))
    s.ctx.log_service = MagicMock()
    s.ctx.log_service.log_notice = AsyncMock()
    on_ready = s.bot.event.call_args.args[0]
    with patch("bot.main.notify_admins_if_idle", new=AsyncMock()):
        await on_ready()
        await on_ready()
    s.ctx.log_service.log_notice.assert_awaited_once()
    assert s.ctx.rebuild_report is None


@pytest.mark.asyncio
async def test_startup_rebuild_runs_instead_of_the_upgrade_import() -> None:
    s = _Startup()
    s.rebuild_instead = True
    await s.run()
    assert "upgrade_import" not in s.calls
    assert s.calls.index("startup_rebuild") < s.calls.index("queue_logos")


@pytest.mark.asyncio
async def test_startup_rebuild_checked_after_recovered_notion() -> None:
    from bot.notion.schema import NotionDatabasesMissing

    s = _Startup(schema_error=NotionDatabasesMissing("gone"))

    async def admin_approves() -> None:
        s._schema_error = None
        assert await s.ctx.notion_recover() is None

    s.while_running = admin_approves
    with patch("bot.main.set_value", new=AsyncMock()):
        await s.run()
    assert s.calls.count("startup_rebuild") == 2
    assert s.calls.index("startup_rebuild") < s.calls.index("upgrade_import")
