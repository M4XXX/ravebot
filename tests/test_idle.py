"""The bot stays idle until it is activated: every gate does nothing while idle."""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.services import activation
from bot.ui.gates import ADMIN_IDLE_REPLY, IDLE_REPLY, STARTUP_ADMIN_DM
from tests.cogs.test_admin import make_mock_ctx, make_mock_inter
from tests.cogs.test_events import _make_cog_with_bot
from tests.cogs.test_onboarding import make_member, make_role
from tests.conftest import make_real_ctx
from tests.notion.factories import make_event
from tests.services.test_lineup import _Ctx, _service


@pytest.fixture
def idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activation, "is_active", AsyncMock(return_value=False))
    monkeypatch.setattr(activation, "bot_is_active", AsyncMock(return_value=False))


# --- loops ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_notion_idle_does_nothing(idle) -> None:
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    await EventsCog._poll_notion.coro(cog)
    cog.ctx.notion_sync_service.run_cycle.assert_not_called()
    cog.ctx.notion_client.list_events.assert_not_called()
    cog.ctx.lineup_service.refresh.assert_not_called()


@pytest.mark.asyncio
async def test_poll_notion_active_runs() -> None:
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.notion_client.list_events = AsyncMock(return_value=[])
    cog.ctx.lineup_service = MagicMock(refresh=AsyncMock())
    with patch("bot.cogs.events.replenish_events", new=AsyncMock(return_value=0)), patch(
        "bot.cogs.events.check_fully_booked", new=AsyncMock()
    ):
        await EventsCog._poll_notion.coro(cog)
    cog.ctx.notion_sync_service.run_cycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_reminders_idle_and_active(monkeypatch) -> None:
    from bot.cogs.events import EventsCog

    cog = _make_cog_with_bot()
    cog.ctx.reminders_service = MagicMock(send_reminders=AsyncMock())
    await EventsCog._check_reminders.coro(cog)
    cog.ctx.reminders_service.send_reminders.assert_awaited_once()

    monkeypatch.setattr(activation, "is_active", AsyncMock(return_value=False))
    cog.ctx.reminders_service.send_reminders.reset_mock()
    await EventsCog._check_reminders.coro(cog)
    cog.ctx.reminders_service.send_reminders.assert_not_called()


@pytest.mark.asyncio
async def test_outreach_check_idle_does_nothing(idle, monkeypatch) -> None:
    from bot.cogs.outreach_cog import OutreachCog
    from tests.cogs.test_outreach_cog import _make_cog

    cog, svc, _ = _make_cog("true")
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.cogs.outreach_cog.outreach_enabled", switch)
    await OutreachCog._outreach_check.coro(cog)
    switch.assert_not_called()
    svc.run_gap_detection.assert_not_called()
    svc.contact_next_dj.assert_not_called()


@pytest.mark.asyncio
async def test_outreach_check_active_runs(monkeypatch) -> None:
    from bot.cogs.outreach_cog import OutreachCog
    from tests.cogs.test_outreach_cog import _make_cog

    cog, svc, _ = _make_cog("true")
    monkeypatch.setattr("bot.cogs.outreach_cog.outreach_enabled", AsyncMock(return_value=True))
    await OutreachCog._outreach_check.coro(cog)
    svc.run_gap_detection.assert_awaited_once()


# --- onboarding ----------------------------------------------------------


@pytest.mark.asyncio
async def test_typed_dm_idle_gets_only_idle_reply(idle) -> None:
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_real_ctx(db=MagicMock())
    ctx.log_service = MagicMock(log_user_message=AsyncMock())
    cog = OnboardingCog(ctx)
    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.bot = False
    message.author.id = 5
    message.content = "hello"
    message.attachments = []
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_awaited_once_with(IDLE_REPLY)
    ctx.log_service.log_user_message.assert_not_called()
    assert not cog._tasks


@pytest.mark.asyncio
async def test_role_assignment_idle_sends_nothing(idle) -> None:
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(MagicMock(db=MagicMock()))
    before, after = make_member(), make_member()
    before.roles = []
    after.roles = [make_role("DJ")]
    with patch("bot.cogs.onboarding.get_dj_by_discord_id", new=AsyncMock(return_value=None)):
        await cog.on_member_update(before, after)
    after.send.assert_not_called()


# --- admin ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "allowed"),
    [("setup", True), ("config show", True), ("create event", False), ("list all djs", False),
     ("book dj slot", False), ("automatic dj booking", False)],
)
async def test_admin_check_idle(idle, name, allowed) -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1)
    inter.application_command.qualified_name = name
    assert await cog.cog_slash_command_check(inter) is allowed
    if allowed:
        inter.response.send_message.assert_not_called()
    else:
        inter.response.send_message.assert_awaited_once_with(ADMIN_IDLE_REPLY, ephemeral=True)


@pytest.mark.asyncio
async def test_admin_check_idle_non_admin_gets_no_idle_text(idle) -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=2)
    inter.application_command.qualified_name = "setup"
    assert await cog.cog_slash_command_check(inter) is False
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_admin_check_active_allows_everything() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1)
    inter.application_command.qualified_name = "create event"
    assert await cog.cog_slash_command_check(inter) is True
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_error_handler_does_not_answer_twice(idle) -> None:
    from disnake.ext import commands

    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1)
    inter.response.is_done = MagicMock(return_value=True)
    await cog.cog_slash_command_error(inter, commands.CheckFailure())
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_config_show_idle_says_setup_not_finished(idle) -> None:
    from bot.cogs.admin import AdminCog
    from bot.ui import texts

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    with patch("bot.cogs.admin.db_config.get_value", new=AsyncMock(return_value=None)):
        await cog.config_show.callback(cog, inter)
    text = inter.response.send_message.call_args.args[0]
    assert text.startswith(texts.SETUP_NOT_FINISHED)
    assert text.endswith("To change a setting, run /setup again.")
    assert "Bot status: idle" in text


# --- lineup --------------------------------------------------------------


@pytest.mark.asyncio
async def test_lineup_posts_nothing_while_idle(idle) -> None:
    event = make_event(event_id="e", page_id="e", date=datetime.date(2026, 11, 14))
    service, cfg, channel, _log, notion = _service()
    with _Ctx(cfg):
        assert await service.refresh([event]) is False
        assert await service.post_event(event) is False
        service.notify_changed()
        await service._task
    channel.send.assert_not_called()
    notion.list_events.assert_not_called()


# --- startup DM ----------------------------------------------------------


def _bot_with_users(fail: set[int] = frozenset()):
    users: dict[int, MagicMock] = {}

    def user(uid: int) -> MagicMock:
        u = MagicMock()
        error = disnake.HTTPException(MagicMock(status=403), "no") if uid in fail else None
        u.send = AsyncMock(side_effect=error)
        users[uid] = u
        return u

    bot = MagicMock(guilds=[])
    bot.get_user = MagicMock(side_effect=user)
    return bot, users


@pytest.mark.asyncio
async def test_startup_dm_goes_to_every_admin_and_swallows_failures(idle) -> None:
    from bot.main import notify_admins_if_idle

    bot, users = _bot_with_users(fail={1})
    await notify_admins_if_idle(bot, MagicMock(), frozenset({1, 2}))
    assert STARTUP_ADMIN_DM == (
        "RaveBot is not set up yet. Run /setup in the channel where you want to do it."
    )
    users[1].send.assert_awaited_once_with(STARTUP_ADMIN_DM)
    users[2].send.assert_awaited_once_with(STARTUP_ADMIN_DM)


@pytest.mark.asyncio
async def test_startup_dm_not_sent_when_active() -> None:
    from bot.main import notify_admins_if_idle

    bot, users = _bot_with_users()
    await notify_admins_if_idle(bot, MagicMock(), frozenset({1}))
    assert users == {}


@pytest.mark.asyncio
async def test_on_ready_sends_startup_dm_once_per_process(idle) -> None:
    from tests.test_main import _Startup

    s = _Startup()
    s.bot.guilds = []
    sent: list[int] = []

    async def fake(bot, db, ids):
        sent.append(1)

    with patch("bot.main.notify_admins_if_idle", new=fake):
        await s.run()
        on_ready = next(
            c.args[0] for c in s.bot.event.call_args_list if c.args[0].__name__ == "on_ready"
        )
        await on_ready()
        await on_ready()
    assert sent == [1]
