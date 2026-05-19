"""Tests for bot/cogs/admin.py — AdminCog slash commands and admin check.

[Source: _bmad-output/implementation-artifacts/1-4-admin-channel-configuration-commands.md#Task 4]
"""
from unittest.mock import AsyncMock, MagicMock

import pytest


def make_mock_ctx(admin_ids: frozenset[int]) -> MagicMock:
    """Build a minimal mock BotContext with config.admin_user_ids set."""
    ctx = MagicMock()
    ctx.config.admin_user_ids = admin_ids
    ctx.db = MagicMock()
    return ctx


def make_mock_inter(user_id: int, guild_id: int = 111) -> MagicMock:
    """Build a minimal mock ApplicationCommandInteraction."""
    inter = MagicMock()
    inter.author.id = user_id
    inter.guild_id = guild_id
    inter.response = AsyncMock()
    return inter


def test_cog_check_allows_admin() -> None:
    """cog_slash_command_check returns True for a user in ADMIN_USER_IDS."""
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({123, 456})))
    inter = make_mock_inter(user_id=123)
    assert cog.cog_slash_command_check(inter) is True


def test_cog_check_rejects_non_admin() -> None:
    """cog_slash_command_check returns False for a user not in ADMIN_USER_IDS."""
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({123})))
    inter = make_mock_inter(user_id=999)
    assert cog.cog_slash_command_check(inter) is False


@pytest.mark.asyncio
async def test_cog_error_responds_on_check_failure() -> None:
    """cog_slash_command_error sends ephemeral permission denial on CheckFailure."""
    from disnake.ext import commands

    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=999)
    await cog.cog_slash_command_error(inter, commands.CheckFailure())
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "permission" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_dj_channel_stores_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """config_set_dj_channel stores 'dj_channel' key via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)

    channel = MagicMock()
    channel.id = 555
    channel.mention = "#dj-lineup"
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    # Call the underlying callback with cog bound as self (disnake SubCommand doesn't auto-bind)
    await cog.config_set_dj_channel.callback(cog, inter, channel)

    assert set_value_calls == [(111, "dj_channel", "555")]
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "DJ channel set to" in call_args[0][0]
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_log_channel_stores_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """config_set_log_channel stores 'log_channel' key via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)

    channel = MagicMock()
    channel.id = 777
    channel.mention = "#admin-log"
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    # Call the underlying callback with cog bound as self (disnake SubCommand doesn't auto-bind)
    await cog.config_set_log_channel.callback(cog, inter, channel)

    assert set_value_calls == [(111, "log_channel", "777")]
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "Log channel set to" in call_args[0][0]
    assert call_args[1].get("ephemeral") is True
