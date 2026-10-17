"""The startup self-check names exactly what is missing in the served server."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot import selfcheck


class Role:
    def __init__(self, name: str, position: int) -> None:
        self.name, self.position = name, position

    def __ge__(self, other: "Role") -> bool:
        return self.position >= other.position


def _perms(**overrides: bool) -> SimpleNamespace:
    base = {p: True for p in selfcheck.CHANNEL_PERMISSIONS}
    base.update(overrides)
    return SimpleNamespace(**base)


def _guild(roles, manage_roles=True, channels=None, bot_position=5):
    channels = channels or {}
    me = SimpleNamespace(
        top_role=Role("RaveBot", bot_position),
        guild_permissions=SimpleNamespace(manage_roles=manage_roles),
    )
    return SimpleNamespace(
        me=me,
        roles=roles,
        get_channel=lambda cid: channels.get(cid),
    )


def _channel(name: str, **perm_overrides: bool) -> SimpleNamespace:
    return SimpleNamespace(name=name, permissions_for=lambda me: _perms(**perm_overrides))


def test_healthy_server_has_no_problems() -> None:
    guild = _guild([Role("DJ", 2), Role("Active Booking", 1)],
                   channels={10: _channel("lineup"), 11: _channel("log")})
    assert selfcheck.check_guild(guild, "10", "11") == []


def test_problems_from_the_live_test_are_named() -> None:
    """What the first live test in a private server ran into, one by one."""
    guild = _guild(
        [Role("dj", 5)],  # same position as the bot role, Active Booking missing
        manage_roles=False,
        channels={10: _channel("testing", view_channel=False, send_messages=False)},
    )
    problems = selfcheck.check_guild(guild, "10", None)
    assert any("Manage Roles" in p for p in problems)
    assert any("'Active Booking' does not exist" in p for p in problems)
    assert any("above 'dj'" in p for p in problems)
    assert any("#testing" in p and "view channel" in p and "send messages" in p for p in problems)
    assert any("No log channel set" in p for p in problems)


@pytest.mark.asyncio
async def test_run_posts_one_notice_with_all_problems() -> None:
    guild = _guild([], manage_roles=False)
    bot = SimpleNamespace(get_guild=lambda gid: guild)
    log = AsyncMock()
    ctx = SimpleNamespace(db=AsyncMock(), log_service=log)
    with patch("bot.db.config.get_value", new=AsyncMock(return_value=None)):
        problems = await selfcheck.run(bot, ctx, 42)
    assert len(problems) >= 4
    log.post_notice.assert_awaited_once()
    assert "RaveBot setup check" in log.post_notice.call_args.args[0]
