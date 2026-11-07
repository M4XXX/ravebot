"""Tests for the /setup command (bot/cogs/setup.py)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from disnake.ext import commands

from bot.cogs.setup import SetupCog
from bot.db.setup_drafts import SetupDraft
from bot.ui import setup_wizard as sw
from bot.ui import texts
from tests.ui.test_setup_wizard import (  # noqa: F401
    ADMIN,
    GUILD,
    Store,
    make_ctx,
    make_guild,
    make_inter,
    store,
)


def _inter(user=ADMIN):
    inter = make_inter(make_guild(), user=user)
    sent = SimpleNamespace(id=777, jump_url="https://x/777")
    inter.edit_original_message = AsyncMock(return_value=sent)
    inter.bot.get_channel = MagicMock(return_value=None)
    return inter


def test_setup_command_qualified_name_and_admin_check():
    cog = SetupCog(make_ctx())
    assert cog.setup.qualified_name == "setup"
    assert cog.cog_slash_command_check(_inter(ADMIN)) is True
    assert cog.cog_slash_command_check(_inter(999)) is False


@pytest.mark.asyncio
async def test_non_admin_is_refused_with_permission_text():
    cog = SetupCog(make_ctx())
    inter = _inter(999)
    await cog.cog_slash_command_error(inter, commands.CheckFailure("x"))
    inter.response.send_message.assert_awaited_once_with(texts.SETUP_NO_PERMISSION, ephemeral=True)


@pytest.mark.asyncio
async def test_setup_posts_step_one_and_saves_draft(store):  # noqa: F811
    cog = SetupCog(make_ctx())
    inter = _inter()
    await cog.setup.callback(cog, inter)
    inter.response.defer.assert_awaited_once()  # a normal, not ephemeral, message
    assert "Step 1 of" in inter.edit_original_message.await_args.kwargs["content"]
    d = store.drafts[GUILD]
    assert (d.message_id, d.channel_id, d.step) == (777, 7, 0)
    assert store.writes == []


@pytest.mark.asyncio
async def test_second_setup_reuses_open_message(store):  # noqa: F811
    cog = SetupCog(make_ctx())
    store.drafts[GUILD] = SetupDraft(GUILD, 777, 7, 1, {})
    inter = _inter()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=SimpleNamespace(jump_url="https://x/777"))
    inter.bot.get_channel = MagicMock(return_value=channel)
    await cog.setup.callback(cog, inter)
    text = inter.response.send_message.await_args.args[0]
    assert text == texts.SETUP_ALREADY_OPEN.format(url="https://x/777")
    assert store.drafts[GUILD].message_id == 777
    inter.edit_original_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_reposts_when_draft_message_is_gone(store):  # noqa: F811
    cog = SetupCog(make_ctx())
    store.drafts[GUILD] = SetupDraft(GUILD, 1, 7, 1, {"dj_channel": 100})
    inter = _inter()
    await cog.setup.callback(cog, inter)
    d = store.drafts[GUILD]
    assert d.message_id == 777 and d.step == 1 and d.data == {"dj_channel": 100}


@pytest.mark.asyncio
async def test_setup_while_active_offers_change_settings(store):  # noqa: F811
    cog = SetupCog(make_ctx())
    store.config[(GUILD, "bot_active")] = "true"
    inter = _inter()
    await cog.setup.callback(cog, inter)
    args, kwargs = inter.response.send_message.await_args
    assert args[0] == (
        "The bot is already active. You can go through the steps again to change settings: "
        "nothing changes until you press Save."
    )
    assert {c.custom_id for c in kwargs["view"].children} == {sw.ID_CHANGE}
    assert kwargs.get("ephemeral") is not True
    assert GUILD not in store.drafts
