"""Admin commands are hidden from regular members, no message can ping, DM mirroring is optional."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.cogs.admin import AdminCog

ADMIN_COMMANDS = (
    "config_base", "intervene_base", "admin_base", "status_cmd", "list_djs", "add_event"
)


@pytest.mark.parametrize("attr", ADMIN_COMMANDS)
def test_admin_commands_need_manage_server_and_a_server(attr: str) -> None:
    body = getattr(AdminCog, attr).body
    assert body.default_member_permissions == disnake.Permissions(manage_guild=True)
    assert body.contexts.guild is True
    assert body.contexts.bot_dm is False
    assert body.contexts.private_channel is False


def test_status_command_has_a_unique_name() -> None:
    assert AdminCog.status_cmd.name == "ravebot-status"


def test_bot_never_pings() -> None:
    from bot.main import create_bot

    mentions = create_bot().allowed_mentions
    assert (mentions.everyone, mentions.users, mentions.roles) == (False, False, False)


def _log_service():
    from bot.log import AdminLogService

    guild = MagicMock()
    guild.id = 42
    bot = MagicMock()
    bot.guilds = [guild]
    svc = AdminLogService(bot=bot, db=AsyncMock())
    channel = MagicMock()
    channel.send = AsyncMock()
    svc._get_log_channel = AsyncMock(return_value=channel)
    return svc, channel


@pytest.mark.asyncio
async def test_dm_log_off_mirrors_nothing() -> None:
    svc, channel = _log_service()
    with patch("bot.log.db_config.get_value", new=AsyncMock(return_value="off")):
        await svc._send_dm_transcript(dj_name="dj", direction="DJ", content="hello")
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_long_dm_is_truncated_below_discord_limit() -> None:
    svc, channel = _log_service()
    with patch("bot.log.db_config.get_value", new=AsyncMock(return_value=None)):
        await svc._send_dm_transcript(dj_name="dj", direction="DJ", content="x" * 5000)
    sent = channel.send.call_args[0][0]
    assert len(sent) < 2000
    assert sent.endswith("[truncated]")
