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

    def __init__(self, schema_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.config = MagicMock()
        self.config.log_level = "INFO"
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
        self._schema_error = schema_error

    async def _start(self, token: str) -> None:
        self.calls.append("start")

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
             patch.object(main_mod, "NotionClient", side_effect=self._make_client):
            self.api_cls = api_cls
            await main_mod.main()


@pytest.mark.asyncio
async def test_startup_ensures_schema_then_views_for_guild_zero() -> None:
    s = _Startup()
    await s.run()

    assert s.calls == ["ensure_schema", "ensure_views", "client", "start"]
    assert s.api_cls.call_args.args[0] == "key"


@pytest.mark.asyncio
async def test_startup_builds_services_with_new_client() -> None:
    from bot.services.notion_sync import NotionSyncService

    s = _Startup()
    await s.run()

    ctx = s.ctx
    assert ctx is not None
    assert ctx.notion_client is s.client
    assert ctx.outreach_service.notion_client is s.client
    assert ctx.reminders_service.notion_client is s.client
    assert ctx.admin_booking_service.notion_client is s.client
    assert isinstance(ctx.notion_sync_service, NotionSyncService)
    assert ctx.notion_sync_service.notion_client is s.client


@pytest.mark.asyncio
async def test_startup_registers_log_reply_view() -> None:
    from bot.ui.log_reply import LogReplyView

    s = _Startup()
    await s.run()
    added = [c.args[0] for c in s.bot.add_view.call_args_list]
    assert any(isinstance(v, LogReplyView) for v in added)


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


async def _ready(s: "_Startup", active: bool, times: int) -> AsyncMock:
    from bot import main as main_mod

    guild = MagicMock()
    s.bot.guilds = [guild]
    retry = AsyncMock(return_value=0)
    with patch.object(main_mod, "retry_unsynced_logos", new=retry), \
         patch.object(main_mod, "load_stored_timezone", new=AsyncMock()), \
         patch.object(main_mod, "notify_admins_if_idle", new=AsyncMock()), \
         patch.object(main_mod.activation, "bot_is_active", new=AsyncMock(return_value=active)):
        on_ready = s.bot.event.call_args.args[0]
        for _ in range(times):
            await on_ready()
    retry.guild = guild
    return retry


@pytest.mark.asyncio
async def test_on_ready_retries_unsynced_logos_once_per_process() -> None:
    s = _Startup()
    await s.run()
    retry = await _ready(s, active=True, times=2)
    retry.assert_awaited_once_with(s.ctx, retry.guild)


@pytest.mark.asyncio
async def test_on_ready_skips_logo_retry_while_idle() -> None:
    s = _Startup()
    await s.run()
    retry = await _ready(s, active=False, times=1)
    retry.assert_not_awaited()
