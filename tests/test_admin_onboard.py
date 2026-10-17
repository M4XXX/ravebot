"""/admin onboard sends the onboarding DM to one chosen DJ; the preview only lists."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.cogs.admin import AdminCog


def _member(member_id: int, name: str, *roles: str, bot: bool = False) -> MagicMock:
    m = MagicMock()
    m.id, m.display_name, m.bot = member_id, name, bot
    role_objs = []
    for r in roles:
        role = MagicMock()
        role.name = r
        role_objs.append(role)
    m.roles = role_objs
    return m


def _inter(onboarding_result: str = "sent") -> MagicMock:
    inter = MagicMock()
    inter.guild_id = 42
    inter.response.send_message = AsyncMock()
    cog = MagicMock()
    cog.send_onboarding_dm = AsyncMock(return_value=onboarding_result)
    inter.client.get_cog = MagicMock(return_value=cog)
    return inter


def _cog() -> AdminCog:
    ctx = MagicMock()
    ctx.db = AsyncMock()
    return AdminCog(ctx)


@pytest.mark.asyncio
async def test_onboard_sends_to_unregistered_dj() -> None:
    inter = _inter()
    dj = _member(7, "Nova", "DJ")
    with patch("bot.cogs.admin.get_dj_by_discord_id", new=AsyncMock(return_value=None)):
        await AdminCog.admin_onboard.callback(_cog(), inter, dj=dj)
    inter.client.get_cog.return_value.send_onboarding_dm.assert_awaited_once_with(dj)
    assert "sent to Nova" in inter.response.send_message.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roles, row, expected",
    [
        (("Guest",), None, "doesn't have the DJ role"),
        (("DJ",), {"onboarding_complete": True}, "already registered"),
    ],
)
async def test_onboard_refuses_without_sending(roles, row, expected) -> None:
    inter = _inter()
    with patch("bot.cogs.admin.get_dj_by_discord_id", new=AsyncMock(return_value=row)):
        await AdminCog.admin_onboard.callback(_cog(), inter, dj=_member(7, "Nova", *roles))
    inter.client.get_cog.return_value.send_onboarding_dm.assert_not_called()
    assert expected in inter.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_onboard_reports_closed_dms() -> None:
    inter = _inter("dms_closed")
    with patch("bot.cogs.admin.get_dj_by_discord_id", new=AsyncMock(return_value=None)):
        await AdminCog.admin_onboard.callback(_cog(), inter, dj=_member(7, "Nova", "DJ"))
    assert "DMs from server members turned off" in inter.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_preview_lists_only_unregistered_dj_role_members() -> None:
    inter = _inter()
    inter.guild.members = [
        _member(1, "Registered", "DJ"),
        _member(2, "Pending", "DJ"),
        _member(3, "Guest", "Guest"),
        _member(4, "SomeBot", "DJ", bot=True),
        _member(5, "Halfway", "DJ"),
    ]
    rows = [
        {"discord_user_id": 1, "onboarding_complete": True},
        {"discord_user_id": 5, "onboarding_complete": False},
    ]
    with patch("bot.cogs.admin.get_all_djs", new=AsyncMock(return_value=rows)):
        await AdminCog.admin_onboard_preview.callback(_cog(), inter)
    text = inter.response.send_message.call_args.args[0]
    assert text.startswith("2 member(s)")
    assert "Halfway" in text and "Pending" in text
    assert "Registered" not in text and "Guest" not in text and "SomeBot" not in text
    inter.client.get_cog.return_value.send_onboarding_dm.assert_not_called()
