"""Cogs get a real `bot` from main._add_cog; the real activation check works with it."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.main import _add_cog, create_bot
from bot.services import activation
from bot.ui.gates import IDLE_REPLY
from tests.cogs.test_onboarding import make_member, make_role
from tests.conftest import make_real_ctx


def _fake_config(monkeypatch, values: dict[str, str]) -> None:
    async def get(db, guild_id, key):
        return values.get(key)

    monkeypatch.setattr(activation, "get_value", get)


@pytest.mark.asyncio
async def test_every_cog_gets_the_bot() -> None:
    from bot.cogs.admin import AdminCog
    from bot.cogs.booking import BookingCog
    from bot.cogs.events import EventsCog
    from bot.cogs.onboarding import OnboardingCog
    from bot.cogs.outreach_cog import OutreachCog

    bot = create_bot()
    ctx = make_real_ctx(db=None)
    cogs = [OnboardingCog(ctx), BookingCog(ctx), OutreachCog(ctx), AdminCog(ctx), EventsCog(ctx)]
    try:
        for cog in cogs:
            _add_cog(bot, cog)
        for cog in cogs:
            assert cog.bot is bot
    finally:
        for cog in cogs:
            bot.remove_cog(type(cog).__name__)


def _bot_with_guild() -> MagicMock:
    return MagicMock(guilds=[MagicMock(id=9)])


@pytest.mark.asyncio
async def test_on_message_with_real_check_active_guild_answers_normally(monkeypatch) -> None:
    from bot.cogs.onboarding import OnboardingCog
    from bot.ui.views import FallbackMenuView

    _fake_config(monkeypatch, {"bot_active": "true"})
    monkeypatch.setattr("bot.cogs.onboarding.get_dj_by_discord_id", AsyncMock(return_value=None))
    cog = OnboardingCog(make_real_ctx(db=MagicMock()))
    cog.bot = _bot_with_guild()
    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.bot = False
    message.author.id = 5
    message.content = ""
    message.attachments = []
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_awaited_once()
    assert isinstance(message.channel.send.call_args.kwargs["view"], FallbackMenuView)


@pytest.mark.asyncio
async def test_on_message_with_real_check_idle_guild_gets_idle_reply(monkeypatch) -> None:
    from bot.cogs.onboarding import OnboardingCog

    _fake_config(monkeypatch, {})
    cog = OnboardingCog(make_real_ctx(db=MagicMock()))
    cog.bot = _bot_with_guild()
    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.bot = False
    message.channel.send = AsyncMock()
    await cog.on_message(message)
    message.channel.send.assert_awaited_once_with(IDLE_REPLY)


@pytest.mark.asyncio
async def test_on_member_update_with_real_check(monkeypatch) -> None:
    from bot.cogs.onboarding import OnboardingCog

    monkeypatch.setattr("bot.cogs.onboarding.get_dj_by_discord_id", AsyncMock(return_value=None))
    for values, sent in (({"dj_channel": "1", "log_channel": "2"}, True), ({}, False)):
        _fake_config(monkeypatch, values)
        cog = OnboardingCog(make_real_ctx(db=MagicMock()))
        cog.bot = _bot_with_guild()
        before, after = make_member(), make_member()
        before.roles = []
        after.roles = [make_role("DJ")]
        after.guild.id = 9
        with patch("bot.cogs.onboarding.OnboardingIntroView", MagicMock()):
            await cog.on_member_update(before, after)
        assert after.send.called is sent
