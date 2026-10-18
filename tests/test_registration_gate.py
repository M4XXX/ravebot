"""Only registered DJs get the booking menu and can book; the DB password never reaches logs."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db.pool import _safe_target

GUILD = 42


def _bot_with_member(member: MagicMock | None) -> MagicMock:
    guild = MagicMock()
    guild.id = GUILD
    guild.get_member = MagicMock(return_value=member)
    bot = MagicMock()
    bot.guilds = [guild]
    return bot


def _member(*role_names: str) -> MagicMock:
    member = MagicMock()
    roles = []
    for name in role_names:
        r = MagicMock()
        r.name = name
        roles.append(r)
    member.roles = roles
    return member


def _dm(author_id: int = 7) -> MagicMock:
    message = MagicMock()
    message.guild = None
    message.author.bot = False
    message.author.id = author_id
    message.channel.send = AsyncMock()
    return message


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.log_service = AsyncMock()
    ctx.get_user_lock = MagicMock(return_value=MagicMock(locked=MagicMock(return_value=False)))
    return ctx


@pytest.mark.asyncio
async def test_dm_from_non_dj_gets_short_notice_not_menu() -> None:
    from bot.cogs.onboarding import NOT_A_DJ_TEXT, OnboardingCog

    cog = OnboardingCog(_ctx(), _bot_with_member(_member("Guest")))
    message = _dm()
    with patch("bot.cogs.onboarding.get_dj_by_discord_id", new=AsyncMock(return_value=None)), patch(
        "bot.cogs.onboarding.is_under_intervention", new=AsyncMock(return_value=False)
    ), patch.object(OnboardingCog, "send_onboarding_dm", new=AsyncMock()) as onboard:
        await cog.on_message(message)
    message.channel.send.assert_awaited_once_with(NOT_A_DJ_TEXT)
    onboard.assert_not_called()


@pytest.mark.asyncio
async def test_dm_from_dj_without_profile_gets_onboarding() -> None:
    from bot.cogs.onboarding import OnboardingCog

    member = _member("DJ")
    cog = OnboardingCog(_ctx(), _bot_with_member(member))
    message = _dm()
    with patch("bot.cogs.onboarding.get_dj_by_discord_id", new=AsyncMock(return_value=None)), patch(
        "bot.cogs.onboarding.is_under_intervention", new=AsyncMock(return_value=False)
    ), patch.object(OnboardingCog, "send_onboarding_dm", new=AsyncMock()) as onboard:
        await cog.on_message(message)
    onboard.assert_awaited_once_with(member)
    message.channel.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row, roles, expected",
    [
        (None, ("DJ",), False),
        ({"onboarding_complete": False}, ("DJ",), False),
        ({"onboarding_complete": True}, ("Guest",), False),
        ({"onboarding_complete": True}, ("DJ",), True),
    ],
)
async def test_is_registered_dj(row, roles, expected) -> None:
    from bot.ui.views import is_registered_dj

    inter = MagicMock()
    inter.author.id = 7
    inter.client = _bot_with_member(_member(*roles))
    with patch("bot.ui.views.get_dj_by_discord_id", new=AsyncMock(return_value=row)):
        assert await is_registered_dj(_ctx(), inter) is expected


@pytest.mark.asyncio
async def test_unregistered_user_cannot_start_booking() -> None:
    from bot.ui.views import NOT_REGISTERED_TEXT, FallbackMenuView

    ctx = _ctx()
    ctx.notion_client = AsyncMock()
    view = FallbackMenuView(ctx)
    inter = MagicMock()
    inter.author.id = 7
    inter.response.send_message = AsyncMock()
    with patch("bot.ui.views.is_registered_dj", new=AsyncMock(return_value=False)):
        await view.book_slot.callback(inter)
    inter.response.send_message.assert_awaited_once_with(NOT_REGISTERED_TEXT, ephemeral=True)
    ctx.notion_client.poll_events.assert_not_called()


def test_safe_target_hides_credentials() -> None:
    target = _safe_target("postgresql://ravebot:s3cr3t-pass@db.example:5433/ravebot")
    assert target == "db.example:5433/ravebot"
    assert "s3cr3t" not in target and "ravebot:" not in target
