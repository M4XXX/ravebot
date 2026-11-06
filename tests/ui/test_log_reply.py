"""Tests for bot/ui/log_reply.py: the Reply button on user message entries."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.ui.log_reply import LogReplyView, ReplyModal
from tests.conftest import make_real_ctx

ADMIN = 111


def _ctx():
    ctx = make_real_ctx(db=MagicMock())
    ctx.config.admin_user_ids = frozenset({ADMIN})
    return ctx


def _click_inter(author_id=ADMIN, message_id=500):
    inter = MagicMock()
    inter.author.id = author_id
    inter.message.id = message_id
    inter.response.send_message = AsyncMock()
    inter.response.send_modal = AsyncMock()
    return inter


def _button(view):
    return view.children[0]


@pytest.mark.asyncio
async def test_view_is_persistent_with_reply_button() -> None:
    view = LogReplyView(_ctx())
    assert view.timeout is None
    assert len(view.children) == 1
    assert _button(view).custom_id == "log:reply"
    assert _button(view).label == "Reply"


@pytest.mark.asyncio
async def test_non_admin_is_refused_privately() -> None:
    view = LogReplyView(_ctx())
    inter = _click_inter(author_id=999)
    await view.reply.callback(inter)
    inter.response.send_message.assert_awaited_once_with(
        "Only organizers can reply.", ephemeral=True
    )
    inter.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_click_opens_modal_for_mapped_user() -> None:
    view = LogReplyView(_ctx())
    inter = _click_inter()
    inter.message.embeds = [disnake.Embed(title="Message from Sam")]
    with patch("bot.ui.log_reply.get_log_reply_user", AsyncMock(return_value=42)) as get:
        await view.reply.callback(inter)
    get.assert_awaited_once()
    assert get.await_args.args[1] == 500
    modal = inter.response.send_modal.await_args.args[0]
    assert isinstance(modal, ReplyModal)
    assert modal.user_id == 42
    assert modal.title == "Reply to Sam"
    field = modal.components[0].children[0]
    assert field.label == "Message"
    assert field.max_length == 2000
    assert field.required is True
    assert field.style == disnake.TextInputStyle.paragraph


@pytest.mark.asyncio
async def test_unknown_message_gives_private_note() -> None:
    view = LogReplyView(_ctx())
    inter = _click_inter()
    with patch("bot.ui.log_reply.get_log_reply_user", AsyncMock(return_value=None)):
        await view.reply.callback(inter)
    inter.response.send_message.assert_awaited_once_with(
        "I cannot find the user for this message.", ephemeral=True
    )
    inter.response.send_modal.assert_not_awaited()


def _modal_inter(text, organizer="Orga"):
    inter = MagicMock(spec=disnake.ModalInteraction)
    inter.author.display_name = organizer
    inter.author.name = organizer
    inter.text_values = {"message": text}
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def _log_message():
    msg = MagicMock(spec=disnake.Message)
    msg.embeds = [disnake.Embed(title="Message from Sam", description="hello")]
    msg.edit = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_submit_success_sends_dm_edits_entry_and_confirms() -> None:
    ctx = _ctx()
    msg = _log_message()
    modal = ReplyModal(ctx, 42, "Sam", msg)
    inter = _modal_inter("x" * 2000)
    with patch("bot.ui.log_reply.dm_user", AsyncMock(return_value=True)) as dm:
        await modal.callback(inter)
    dm.assert_awaited_once_with(inter.bot, 42, "x" * 2000)
    kwargs = msg.edit.await_args.kwargs
    assert kwargs["view"] is None
    embed = kwargs["embed"]
    assert embed.title == "Message from Sam"
    assert embed.description == "hello"
    assert embed.fields[0].name == "Orga replied"
    assert embed.fields[0].value.startswith("x" * 900)
    assert embed.fields[0].value.endswith("(cut)")
    sent = (
        inter.followup.send.await_args if inter.followup.send.await_count
        else inter.response.send_message.await_args
    )
    assert sent.args == ("Sent.",)
    assert sent.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_submit_dm_failure_leaves_entry_untouched() -> None:
    msg = _log_message()
    modal = ReplyModal(_ctx(), 42, "Sam", msg)
    inter = _modal_inter("hi")
    with patch("bot.ui.log_reply.dm_user", AsyncMock(return_value=False)):
        await modal.callback(inter)
    msg.edit.assert_not_awaited()
    sent = (
        inter.followup.send.await_args if inter.followup.send.await_count
        else inter.response.send_message.await_args
    )
    assert sent.args == (
        "I could not send the message. The user may have DMs from server members turned off.",
    )
    assert sent.kwargs["ephemeral"] is True


def test_modal_custom_id_is_unique_per_log_entry() -> None:
    ctx = _ctx()
    m1, m2 = _log_message(), _log_message()
    m1.id, m2.id = 701, 702
    a = ReplyModal(ctx, 1, "A", m1)
    b = ReplyModal(ctx, 2, "B", m2)
    assert a.custom_id == "log_reply_modal:701"
    assert b.custom_id == "log_reply_modal:702"
    assert a.custom_id != b.custom_id


@pytest.mark.asyncio
async def test_two_open_modals_each_dm_their_own_user() -> None:
    ctx = _ctx()
    m1, m2 = _log_message(), _log_message()
    m1.id, m2.id = 701, 702
    a = ReplyModal(ctx, 1, "A", m1)
    b = ReplyModal(ctx, 2, "B", m2)
    with patch("bot.ui.log_reply.dm_user", AsyncMock(return_value=True)) as dm:
        await a.callback(_modal_inter("to a"))
        await b.callback(_modal_inter("to b"))
    assert [c.args[1:] for c in dm.await_args_list] == [(1, "to a"), (2, "to b")]


@pytest.mark.asyncio
async def test_submit_refetches_entry_so_earlier_reply_is_kept() -> None:
    stale = _log_message()
    fresh = _log_message()
    fresh.embeds = [
        disnake.Embed(title="Message from Sam", description="hello").add_field(
            name="Other replied", value="first", inline=False
        )
    ]
    fresh.edit = AsyncMock()
    stale.channel = MagicMock()
    stale.channel.fetch_message = AsyncMock(return_value=fresh)
    stale.id = 5
    modal = ReplyModal(_ctx(), 42, "Sam", stale)
    with patch("bot.ui.log_reply.dm_user", AsyncMock(return_value=True)):
        await modal.callback(_modal_inter("second"))
    stale.edit.assert_not_awaited()
    embed = fresh.edit.await_args.kwargs["embed"]
    assert [f.name for f in embed.fields] == ["Other replied", "Orga replied"]


@pytest.mark.asyncio
async def test_submit_refetch_failure_falls_back_to_stale_copy() -> None:
    stale = _log_message()
    stale.channel = MagicMock()
    stale.channel.fetch_message = AsyncMock(side_effect=disnake.NotFound(MagicMock(), "gone"))
    stale.id = 5
    modal = ReplyModal(_ctx(), 42, "Sam", stale)
    with patch("bot.ui.log_reply.dm_user", AsyncMock(return_value=True)):
        await modal.callback(_modal_inter("second"))
    assert stale.edit.await_args.kwargs["embed"].fields[0].name == "Orga replied"


@pytest.mark.asyncio
async def test_long_reply_is_cut_then_escaped_with_note() -> None:
    msg = _log_message()
    modal = ReplyModal(_ctx(), 42, "Sam", msg)
    with patch("bot.ui.log_reply.dm_user", AsyncMock(return_value=True)):
        await modal.callback(_modal_inter("*" * 2000))
    value = msg.edit.await_args.kwargs["embed"].fields[0].value
    assert len(value) <= 1024
    assert value.endswith("(cut)")
    assert "\\*" in value
    assert not value.removesuffix(" (cut)").endswith("\\")
