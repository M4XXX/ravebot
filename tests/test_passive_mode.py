"""Passive mode and guild pinning — a fresh install joins a server without contacting anyone.

These tests run without the conftest fixture that marks the bot active, so they exercise the
real switch (bot/active.py, default off) and the real guild lookup (bot/guild.py).
"""
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import active
from bot import guild as served

pytestmark = pytest.mark.passive

GUILD = 42


def _bot(*guild_ids: int) -> MagicMock:
    bot = MagicMock()
    guilds = []
    for gid in guild_ids:
        g = MagicMock()
        g.id = gid
        guilds.append(g)
    bot.guilds = guilds
    return bot


@pytest.fixture(autouse=True)
def _unpinned():
    served.configure(None)
    yield
    served.configure(None)


# --- bot/guild.py --------------------------------------------------------------------------


def test_single_guild_is_served_without_pin() -> None:
    assert served.resolve(_bot(GUILD)) == GUILD


def test_several_guilds_without_pin_serve_none() -> None:
    assert served.resolve(_bot(1, 2)) is None


def test_pinned_guild_is_served_among_several() -> None:
    served.configure(2)
    assert served.resolve(_bot(1, 2, 3)) == 2
    assert served.serves(_bot(1, 2, 3), 2) is True
    assert served.serves(_bot(1, 2, 3), 1) is False


def test_pinned_guild_missing_serves_none() -> None:
    served.configure(99)
    assert served.resolve(_bot(1)) is None
    assert served.served_guilds(_bot(1)) == []


# --- bot/active.py -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_is_passive() -> None:
    with patch("bot.active.get_value", new=AsyncMock(return_value=None)):
        assert await active.is_active(AsyncMock(), GUILD) is False


@pytest.mark.asyncio
async def test_on_is_active_and_missing_pool_or_guild_is_passive() -> None:
    with patch("bot.active.get_value", new=AsyncMock(return_value="on")):
        assert await active.is_active(AsyncMock(), GUILD) is True
        assert await active.is_active(None, GUILD) is False
        assert await active.is_active(AsyncMock(), None) is False


# --- gates ---------------------------------------------------------------------------------


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.log_service = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_passive_dj_role_sends_no_onboarding_dm() -> None:
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(_ctx(), _bot(GUILD))
    dj = MagicMock()
    dj.name = "DJ"
    before, after = MagicMock(), MagicMock()
    before.roles, after.roles = [], [dj]
    after.guild.id = GUILD
    after.send = AsyncMock()
    with patch("bot.active.get_value", new=AsyncMock(return_value=None)), patch(
        "bot.cogs.onboarding.get_dj_by_discord_id", new=AsyncMock(return_value=None)
    ):
        await cog.on_member_update(before, after)
    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_role_change_in_unserved_guild_is_ignored_even_when_active() -> None:
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(_ctx(), _bot(GUILD))
    dj = MagicMock()
    dj.name = "DJ"
    before, after = MagicMock(), MagicMock()
    before.roles, after.roles = [], [dj]
    after.guild.id = 7  # not the served guild
    after.send = AsyncMock()
    with patch("bot.active.get_value", new=AsyncMock(return_value="on")), patch(
        "bot.cogs.onboarding.get_dj_by_discord_id", new=AsyncMock(return_value=None)
    ):
        await cog.on_member_update(before, after)
    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_passive_dm_gets_no_reply_and_no_transcript() -> None:
    from bot.cogs.onboarding import OnboardingCog

    ctx = _ctx()
    cog = OnboardingCog(ctx, _bot(GUILD))
    message = MagicMock()
    message.guild = None
    message.author.bot = False
    message.channel.send = AsyncMock()
    with patch("bot.active.get_value", new=AsyncMock(return_value=None)):
        await cog.on_message(message)
    message.channel.send.assert_not_called()
    ctx.log_service.post_dm_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_passive_view_buttons_are_refused() -> None:
    from bot.ui.views import FallbackMenuView

    view = FallbackMenuView(_ctx())
    inter = MagicMock()
    inter.client = _bot(GUILD)
    inter.response.send_message = AsyncMock()
    with patch("bot.active.get_value", new=AsyncMock(return_value=None)):
        assert await view.interaction_check(inter) is False
    inter.response.send_message.assert_awaited_once_with(active.DJ_NOTICE, ephemeral=True)

    inter.response.send_message.reset_mock()
    with patch("bot.active.get_value", new=AsyncMock(return_value="on")):
        assert await view.interaction_check(inter) is True
    inter.response.send_message.assert_not_called()


def test_every_dj_facing_view_is_gated() -> None:
    from bot.ui import views

    view_classes = [
        obj for _, obj in inspect.getmembers(views, inspect.isclass)
        if issubclass(obj, views.disnake.ui.View) and obj.__module__ == views.__name__
    ]
    ungated = [c.__name__ for c in view_classes if not issubclass(c, views.ActiveOnlyView)]
    assert ungated == []


@pytest.mark.asyncio
async def test_passive_admin_write_commands_are_refused() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(_ctx())
    inter = MagicMock()
    inter.guild_id = GUILD
    inter.response.send_message = AsyncMock()
    with patch("bot.active.get_value", new=AsyncMock(return_value=None)):
        assert await cog._refuse_if_passive(inter) is True
    inter.response.send_message.assert_awaited_once_with(active.PASSIVE_NOTICE, ephemeral=True)

    for name in ("intervene_start", "intervene_send", "admin_book", "admin_cancel",
                 "admin_dj_set_active", "add_event", "admin_onboard"):
        source = inspect.getsource(getattr(AdminCog, name).callback)
        assert "self._refuse_if_passive(inter)" in source, name


@pytest.mark.asyncio
async def test_passive_poll_creates_no_events_and_posts_no_lineup() -> None:
    from bot.cogs.events import EventsCog

    ctx = _ctx()
    ctx.notion_client = AsyncMock()
    ctx.lineup_service = AsyncMock()
    cog = EventsCog(ctx, _bot(GUILD))
    with patch("bot.active.get_value", new=AsyncMock(return_value=None)), patch(
        "bot.schedule.refresh", new=AsyncMock()
    ), patch.object(EventsCog, "_guard_db", new=AsyncMock(return_value=True)):
        await EventsCog._poll_notion.coro(cog)
    ctx.notion_client.poll_events.assert_not_called()
    ctx.notion_client.create_event.assert_not_called()
    ctx.lineup_service.post_update.assert_not_called()


@pytest.mark.asyncio
async def test_passive_outreach_and_reminders_do_nothing() -> None:
    from bot.cogs.events import EventsCog
    from bot.cogs.outreach_cog import OutreachCog

    ctx = _ctx()
    ctx.reminders_service = AsyncMock()
    ctx.outreach_service = AsyncMock()
    ctx.notion_client = AsyncMock()
    with patch("bot.active.get_value", new=AsyncMock(return_value=None)), patch.object(
        EventsCog, "_guard_db", new=AsyncMock(return_value=True)
    ), patch.object(OutreachCog, "_guard_db", new=AsyncMock(return_value=True)):
        await EventsCog._check_reminders.coro(EventsCog(ctx, _bot(GUILD)))
        await OutreachCog._outreach_check.coro(OutreachCog(ctx, _bot(GUILD)))
    ctx.reminders_service.send_reminders.assert_not_called()
    ctx.notion_client.poll_events.assert_not_called()
    assert ctx.outreach_service.mock_calls == []
