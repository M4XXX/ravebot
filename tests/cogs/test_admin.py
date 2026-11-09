"""Tests for bot/cogs/admin.py — AdminCog slash commands and admin check.

[Source: _bmad-output/implementation-artifacts/1-4-admin-channel-configuration-commands.md#Task 4]
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from disnake.ext import commands


def make_mock_ctx(admin_ids: frozenset[int]) -> MagicMock:
    """Build a minimal mock BotContext with config.admin_user_ids set."""
    ctx = MagicMock()
    ctx.config.admin_user_ids = admin_ids
    ctx.is_admin = lambda user_id: user_id in admin_ids
    ctx.db = MagicMock()
    return ctx


def make_mock_inter(user_id: int, guild_id: int = 111) -> MagicMock:
    """Build a minimal mock ApplicationCommandInteraction.

    ``inter.order`` records "defer", "followup" and "send_message" in call order.
    """
    inter = MagicMock()
    inter.author.id = user_id
    inter.guild_id = guild_id
    inter.response = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.order = []
    inter.response.defer.side_effect = lambda *a, **k: inter.order.append("defer")
    inter.response.send_message.side_effect = (
        lambda *a, **k: inter.order.append("send_message")
    )
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock(side_effect=lambda *a, **k: inter.order.append("followup"))
    return inter


def assert_deferred_reply(inter: MagicMock) -> tuple[str, dict]:
    """The handler deferred (ephemeral) first and replied once via followup; return it."""
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.response.send_message.assert_not_awaited()
    inter.followup.send.assert_awaited_once()
    assert inter.order == ["defer", "followup"]
    args, kwargs = inter.followup.send.call_args
    assert kwargs.get("ephemeral") is True
    return args[0], kwargs


@pytest.mark.asyncio
async def test_cog_check_allows_admin() -> None:
    """cog_slash_command_check returns True for a user in ADMIN_USER_IDS."""
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({123, 456})))
    inter = make_mock_inter(user_id=123)
    assert await cog.cog_slash_command_check(inter) is True


@pytest.mark.asyncio
async def test_cog_check_rejects_non_admin() -> None:
    """cog_slash_command_check returns False for a user not in ADMIN_USER_IDS."""
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({123})))
    inter = make_mock_inter(user_id=999)
    assert await cog.cog_slash_command_check(inter) is False


async def _registry_ctx(added: int):
    from bot.context import BotContext
    from bot.services.admin_registry import AdminRegistry

    ctx = BotContext(config=MagicMock(admin_user_ids=frozenset({123})))
    ctx.db = MagicMock()
    ctx.admins = AdminRegistry(frozenset({123}), ctx.db)
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)):
        await ctx.admins.add(added)
    return ctx


@pytest.mark.asyncio
async def test_cog_check_accepts_an_added_admin_and_rejects_a_stranger() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(await _registry_ctx(777))
    assert await cog.cog_slash_command_check(make_mock_inter(user_id=777)) is True
    assert await cog.cog_slash_command_check(make_mock_inter(user_id=999)) is False


@pytest.mark.asyncio
async def test_event_autocomplete_without_a_guild_returns_empty(monkeypatch) -> None:
    cog, _ = _lineup_cog(monkeypatch, [_lineup_event("E1", 3)])
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=True))
    inter = make_mock_inter(user_id=123)
    inter.guild_id = None
    assert await cog.post_event_autocomplete(inter, "") == []


@pytest.mark.asyncio
async def test_registry_add_and_remove_change_the_next_cog_check() -> None:
    from bot.cogs.admin import AdminCog

    ctx = await _registry_ctx(777)  # 777 is added through the registry
    cog = AdminCog(ctx)
    assert await cog.cog_slash_command_check(make_mock_inter(user_id=777)) is True
    with patch("bot.services.admin_registry.remove_web_admin", AsyncMock(return_value=True)):
        assert await ctx.admins.remove(777) is True
    assert await cog.cog_slash_command_check(make_mock_inter(user_id=777)) is False
    with patch("bot.services.admin_registry.add_web_admin", AsyncMock(return_value=True)):
        await ctx.admins.add(777)
    assert await cog.cog_slash_command_check(make_mock_inter(user_id=777)) is True


@pytest.mark.asyncio
async def test_event_autocomplete_accepts_an_added_admin(monkeypatch) -> None:
    cog, _ = _lineup_cog(monkeypatch, [_lineup_event("E1", 3)])
    registry_ctx = await _registry_ctx(777)
    cog.ctx.is_admin = registry_ctx.is_admin
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=True))
    assert await cog.post_event_autocomplete(make_mock_inter(user_id=777), "") == ["E1"]
    assert await cog.post_event_autocomplete(make_mock_inter(user_id=999), "") == []


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
@pytest.mark.asyncio
# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def make_mock_member(user_id: int = 42, display_name: str = "DJ Test") -> MagicMock:
    """Build a minimal mock disnake.Member."""
    member = MagicMock()
    member.id = user_id
    member.display_name = display_name
    member.mention = f"<@{user_id}>"
    return member


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
# ---------------------------------------------------------------------------
# /book dj slot and /cancel dj slot tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_book_calls_service_and_replies() -> None:
    """admin_book calls book_slot_override and sends success reply on (True, '')."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    ctx.admin_booking_service.book_slot_override = AsyncMock(
        return_value=(True, ""), side_effect=lambda **k: inter.order.append("service") or (True, "")
    )
    dj = make_mock_member(user_id=42, display_name="DJ Scope")

    await cog.book_dj_slot.callback(cog, inter, dj, "Rave Night #12", 2)

    ctx.admin_booking_service.book_slot_override.assert_awaited_once_with(
        admin_name=inter.author.name,
        dj_id=42,
        dj_name="DJ Scope",
        event_name="Rave Night #12",
        slot_num=2,
    )
    assert inter.order[:2] == ["defer", "service"]
    inter.order.remove("service")
    reply, _ = assert_deferred_reply(inter)
    assert "done" in reply.lower()


@pytest.mark.asyncio
async def test_admin_book_service_returns_error_replies_with_error() -> None:
    """admin_book sends the service error message when service returns (False, msg)."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.book_slot_override = AsyncMock(
        return_value=(False, "Event 'Ghost' not found in Notion.")
    )

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=42)

    await cog.book_dj_slot.callback(cog, inter, dj, "Ghost", 1)

    reply, _ = assert_deferred_reply(inter)
    assert "not found" in reply.lower()


@pytest.mark.asyncio
async def test_admin_book_returns_not_ready_when_service_none() -> None:
    """admin_book replies 'not ready' when admin_booking_service is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member()

    await cog.book_dj_slot.callback(cog, inter, dj, "Event", 1)

    inter.response.send_message.assert_awaited_once()
    assert "not ready" in inter.response.send_message.call_args[0][0].lower()
    inter.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_cancel_calls_service_and_replies() -> None:
    """admin_cancel calls cancel_slot_override and sends success reply on (True, '')."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    ctx.admin_booking_service.cancel_slot_override = AsyncMock(
        side_effect=lambda **k: inter.order.append("service") or (True, "")
    )
    dj = make_mock_member(user_id=7, display_name="DJ Wave")

    await cog.cancel_dj_slot.callback(cog, inter, dj, "Rave Night #12", 1)

    ctx.admin_booking_service.cancel_slot_override.assert_awaited_once_with(
        admin_name=inter.author.name,
        dj_id=7,
        dj_name="DJ Wave",
        event_name="Rave Night #12",
        slot_num=1,
    )
    assert inter.order[:2] == ["defer", "service"]
    inter.order.remove("service")
    reply, _ = assert_deferred_reply(inter)
    assert "done" in reply.lower()


@pytest.mark.asyncio
async def test_admin_cancel_no_booking_replies_with_message() -> None:
    """admin_cancel relays 'No booking found' error from service."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.cancel_slot_override = AsyncMock(
        return_value=(False, "No booking found for @DJ Wave in that slot.")
    )

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=7, display_name="DJ Wave")

    await cog.cancel_dj_slot.callback(cog, inter, dj, "Rave Night #12", 1)

    reply, _ = assert_deferred_reply(inter)
    assert "no booking found" in reply.lower()


# ---------------------------------------------------------------------------
# /automatic dj booking tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_dj_set_active_true_calls_service_and_replies() -> None:
    """automatic_dj_booking with active=True calls set_dj_active_status and replies."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.set_dj_active_status = AsyncMock(return_value=(True, ""))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member(user_id=42, display_name="DJ Scope")

    await cog.automatic_dj_booking.callback(cog, inter, dj, True)

    ctx.admin_booking_service.set_dj_active_status.assert_awaited_once_with(
        111, 42, "DJ Scope", True
    )
    call_args = inter.response.send_message.call_args
    assert "active" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_dj_set_active_false_calls_service_and_replies() -> None:
    """automatic_dj_booking with active=False calls set_dj_active_status and replies."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.set_dj_active_status = AsyncMock(return_value=(True, ""))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member(user_id=42, display_name="DJ Exit")

    await cog.automatic_dj_booking.callback(cog, inter, dj, False)

    ctx.admin_booking_service.set_dj_active_status.assert_awaited_once_with(
        111, 42, "DJ Exit", False
    )
    call_args = inter.response.send_message.call_args
    assert "removed" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_dj_set_active_dj_not_registered_replies_with_error() -> None:
    """automatic_dj_booking relays 'not registered' error from service."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.set_dj_active_status = AsyncMock(
        return_value=(False, "@DJ Ghost is not registered in the system.")
    )

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member(user_id=99, display_name="DJ Ghost")

    await cog.automatic_dj_booking.callback(cog, inter, dj, True)

    call_args = inter.response.send_message.call_args
    assert "not registered" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_dj_set_active_not_ready_when_service_none() -> None:
    """automatic_dj_booking replies 'not ready' when admin_booking_service is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member()

    await cog.automatic_dj_booking.callback(cog, inter, dj, True)

    inter.response.send_message.assert_awaited_once()
    assert "not ready" in inter.response.send_message.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# /list all djs tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_list_djs_returns_embed_with_all_djs(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_djs builds a NEBULA_WHITE embed listing all registered DJs."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    monkeypatch.setattr(
        "bot.cogs.admin.get_all_djs",
        AsyncMock(return_value=[
            {"discord_user_id": 42, "dj_name": "DJ Scope", "active_booking": True},
            {"discord_user_id": 99, "dj_name": "DJ Wave", "active_booking": False},
        ]),
    )

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.list_all_djs.callback(cog, inter)

    call_kwargs = inter.response.send_message.call_args[1]
    assert call_kwargs.get("ephemeral") is True
    embed = call_kwargs.get("embed")
    assert embed is not None
    assert "DJ Scope" in embed.description
    assert "Active" in embed.description
    assert "Inactive" in embed.description


@pytest.mark.asyncio
async def test_list_djs_no_djs_replies_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_djs replies with plain text when no DJs are registered."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    monkeypatch.setattr("bot.cogs.admin.get_all_djs", AsyncMock(return_value=[]))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.list_all_djs.callback(cog, inter)

    assert "no djs registered" in inter.response.send_message.call_args[0][0].lower()
    assert inter.response.send_message.call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_list_djs_not_ready_when_db_none() -> None:
    """list_djs replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.list_all_djs.callback(cog, inter)

    assert "not ready" in inter.response.send_message.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# /create event tests
# ---------------------------------------------------------------------------


def _add_event_patches(monkeypatch: pytest.MonkeyPatch, shape=None):
    """Patch default_event_shape and init_event_slots in the admin cog; return (shape, init)."""
    import datetime

    from bot.services.event_defaults import EventShape

    shape = shape or EventShape(start=datetime.time(19, 30), slot_minutes=45, slot_count=5)
    shape_mock = AsyncMock(return_value=shape)
    init = AsyncMock()
    monkeypatch.setattr("bot.cogs.admin.default_event_shape", shape_mock)
    monkeypatch.setattr("bot.cogs.admin.init_event_slots", init)
    monkeypatch.setattr("bot.cogs.admin.mark_manual", AsyncMock())
    return shape_mock, init


@pytest.mark.asyncio
async def test_add_event_creates_event_and_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_event creates '<date> Rave Night' with the configured shape and inits slot locks."""
    import datetime

    from bot.cogs.admin import AdminCog
    from tests.notion.factories import make_event

    future_date = datetime.date.today() + datetime.timedelta(days=7)
    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(
        return_value=make_event(
            page_id="new-evt-page", event_id="x", date=future_date, start=datetime.time(19, 30)
        )
    )
    shape_mock, init = _add_event_patches(monkeypatch)

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    shape = shape_mock.return_value
    shape_mock.side_effect = lambda *a: inter.order.append("shape") or shape

    future_date = datetime.date.today() + datetime.timedelta(days=7)
    date_str = future_date.isoformat()

    await cog.create_event.callback(cog, inter, date_str, None, None, None)
    assert inter.order[:2] == ["defer", "shape"]
    inter.order.remove("shape")

    shape_mock.assert_awaited_once_with(ctx.db, 111)
    ctx.notion_client.create_event.assert_awaited_once_with(
        f"{future_date.isoformat()} Rave Night",
        future_date,
        datetime.time(19, 30),
        5,
        45,
    )
    init.assert_awaited_once_with(ctx.db, "new-evt-page", 5)
    created = ctx.notion_client.create_event.return_value
    reply, _ = assert_deferred_reply(inter)
    from bot.ui import texts

    assert "<t:" not in reply
    assert reply == f"Event created in Notion for {texts.fmt_slot_when(created, 0)}."


@pytest.mark.asyncio
async def test_add_event_duplicate_id_replies_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime

    from bot.cogs.admin import AdminCog
    from bot.notion.client import DuplicateEventError

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(side_effect=DuplicateEventError("dup"))
    _, init = _add_event_patches(monkeypatch)

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    future_date = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    await cog.create_event.callback(cog, inter, future_date, None, None, None)

    assert_deferred_reply(inter)
    inter.followup.send.assert_awaited_once_with(
        "An event with that ID already exists.", ephemeral=True
    )
    init.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_event_init_slots_failure_still_replies_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init_event_slots failing is non-fatal (slots are created lazily on booking)."""
    import datetime

    from bot.cogs.admin import AdminCog
    from tests.notion.factories import make_event

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(return_value=make_event(page_id="p"))
    _, init = _add_event_patches(monkeypatch)
    init.side_effect = RuntimeError("db down")

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    future_date = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    await cog.create_event.callback(cog, inter, future_date, None, None, None)

    reply, _ = assert_deferred_reply(inter)
    assert "Event created in Notion" in reply


@pytest.mark.asyncio
async def test_add_event_past_date_replies_error() -> None:
    """add_event rejects a past date with 'invalid date' reply and does not call create_event."""
    import datetime

    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock()

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    past_date = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    await cog.create_event.callback(cog, inter, past_date, None, None, None)

    ctx.notion_client.create_event.assert_not_called()
    reply = inter.response.send_message.call_args[0][0].lower()
    assert "invalid date" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True
    inter.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_event_invalid_format_replies_error() -> None:
    """add_event rejects a non-date string with 'invalid date' reply."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock()

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.create_event.callback(cog, inter, "not-a-date", None, None, None)

    ctx.notion_client.create_event.assert_not_called()
    reply = inter.response.send_message.call_args[0][0].lower()
    assert "invalid date" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True
    inter.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_event_not_ready_when_notion_none() -> None:
    """add_event replies 'not ready' when notion_client is None."""
    import datetime

    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    future_date = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    await cog.create_event.callback(cog, inter, future_date, None, None, None)

    reply = inter.response.send_message.call_args[0][0].lower()
    assert "not ready" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_add_event_notion_write_error_replies_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_event replies 'failed to create' when create_event raises NotionWriteError."""
    import datetime

    from bot.cogs.admin import AdminCog
    from bot.notion.client import NotionWriteError

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(side_effect=NotionWriteError("write failed"))
    _add_event_patches(monkeypatch)

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    future_date = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    await cog.create_event.callback(cog, inter, future_date, None, None, None)

    reply, _ = assert_deferred_reply(inter)
    assert "could not create the event" in reply.lower()


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
# ---------------------------------------------------------------------------
# /config show tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_config_show_db_none_replies_not_ready() -> None:
    """config_show replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_show.callback(cog, inter)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
# ---------------------------------------------------------------------------
# /config show field count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_cog_error_after_defer_sends_followup() -> None:
    """An unexpected error after defer is answered via followup, not send_message."""
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1)
    inter.response.is_done = MagicMock(return_value=True)
    await cog.cog_slash_command_error(inter, RuntimeError("boom"))
    inter.followup.send.assert_awaited_once_with(
        "Something went wrong. Please try again in a moment.", ephemeral=True
    )
    inter.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cog_error_before_response_uses_send_message() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1)
    inter.response.is_done = MagicMock(return_value=False)
    await cog.cog_slash_command_error(inter, RuntimeError("boom"))
    inter.response.send_message.assert_awaited_once_with(
        "Something went wrong. Please try again in a moment.", ephemeral=True
    )
    inter.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cog_error_followup_http_exception_does_not_propagate() -> None:
    import disnake

    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1)
    inter.response.is_done = MagicMock(return_value=True)
    inter.followup.send = AsyncMock(
        side_effect=disnake.HTTPException(MagicMock(status=500, reason="x"), "boom")
    )
    await cog.cog_slash_command_error(inter, RuntimeError("boom"))
    inter.followup.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# /post lineup update
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402

_TODAY = _dt.date(2026, 11, 6)


def _lineup_event(event_id: str, days: int, status: str | None = "Planned"):
    from bot.notion.models import EventInfo

    return EventInfo(
        page_id=f"page-{event_id}",
        event_id=event_id,
        date=_TODAY + _dt.timedelta(days=days),
        status=status,
        genre=None,
        notes=None,
        slot_count=2,
        start=_dt.time(20, 0),
        slot_minutes=60,
        slots=(),
    )


def _lineup_cog(monkeypatch: pytest.MonkeyPatch, events, post_ok: bool = True):
    from bot.cogs import admin as admin_mod
    from bot.cogs.admin import AdminCog

    monkeypatch.setattr(admin_mod, "_today", lambda: _TODAY)
    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=events)
    ctx.lineup_service = MagicMock()
    ctx.lineup_service.post_event = AsyncMock(return_value=post_ok)
    return AdminCog(ctx), ctx


@pytest.mark.asyncio
async def test_lineup_post_defaults_to_upcoming(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _lineup_event("E1", 3), _lineup_event("E2", 10)
    cog, ctx = _lineup_cog(monkeypatch, [second, first])
    inter = make_mock_inter(user_id=1)
    await cog.post_lineup_update.callback(cog, inter, None)
    ctx.lineup_service.post_event.assert_awaited_once_with(first)
    assert assert_deferred_reply(inter)[0] == "Lineup posted."


@pytest.mark.asyncio
async def test_lineup_post_specific_event_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _lineup_event("E1", 3), _lineup_event("E2", 10)
    cog, ctx = _lineup_cog(monkeypatch, [first, second])
    inter = make_mock_inter(user_id=1)
    await cog.post_lineup_update.callback(cog, inter, "e2")
    ctx.lineup_service.post_event.assert_awaited_once_with(second)
    assert assert_deferred_reply(inter)[0] == "Lineup posted."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _lineup_event("UNKNOWN-NOT-IN-LIST", 5).event_id + "x",
        "DONE1",
        "CANC1",
        "PAST1",
    ],
)
async def test_lineup_post_unavailable_event(
    monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    events = [
        _lineup_event("E1", 3),
        _lineup_event("DONE1", 4, "Done"),
        _lineup_event("CANC1", 5, "Canceled"),
        _lineup_event("PAST1", -2),
    ]
    cog, ctx = _lineup_cog(monkeypatch, events)
    inter = make_mock_inter(user_id=1)
    await cog.post_lineup_update.callback(cog, inter, event)
    ctx.lineup_service.post_event.assert_not_awaited()
    text = assert_deferred_reply(inter)[0]
    assert text == "That event is not available. Pick one from the list."


@pytest.mark.asyncio
async def test_lineup_post_no_future_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.ui import texts

    cog, ctx = _lineup_cog(monkeypatch, [_lineup_event("PAST1", -2)])
    inter = make_mock_inter(user_id=1)
    await cog.post_lineup_update.callback(cog, inter, None)
    ctx.lineup_service.post_event.assert_not_awaited()
    assert assert_deferred_reply(inter)[0] == texts.NO_LINEUP


@pytest.mark.asyncio
async def test_lineup_post_failure_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    cog, _ = _lineup_cog(monkeypatch, [_lineup_event("E1", 3)], post_ok=False)
    inter = make_mock_inter(user_id=1)
    await cog.post_lineup_update.callback(cog, inter, None)
    text = assert_deferred_reply(inter)[0]
    assert "—" not in text
    assert text != "Lineup posted."


@pytest.mark.asyncio
async def test_lineup_post_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    cog, ctx = _lineup_cog(monkeypatch, [])
    ctx.lineup_service = None
    inter = make_mock_inter(user_id=1)
    await cog.post_lineup_update.callback(cog, inter, None)
    text = inter.response.send_message.call_args[0][0]
    assert "not ready" in text.lower() and "—" not in text


@pytest.mark.asyncio
async def test_lineup_post_notion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cog, ctx = _lineup_cog(monkeypatch, [])
    ctx.notion_client.list_events = AsyncMock(side_effect=RuntimeError("boom"))
    inter = make_mock_inter(user_id=1)
    await cog.post_lineup_update.callback(cog, inter, None)
    ctx.lineup_service.post_event.assert_not_awaited()
    assert "—" not in assert_deferred_reply(inter)[0]


@pytest.mark.asyncio
async def test_lineup_autocomplete_filters_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _lineup_event("RAVE-1", 3),
        _lineup_event("RAVE-2", 4),
        _lineup_event("OTHER", 5),
        _lineup_event("RAVE-DONE", 6, "Done"),
        _lineup_event("RAVE-OLD", -1),
    ]
    cog, _ = _lineup_cog(monkeypatch, events)
    inter = make_mock_inter(user_id=1)
    assert await cog.post_event_autocomplete(inter, "rave") == ["RAVE-1", "RAVE-2"]
    assert await cog.post_event_autocomplete(inter, "") == ["RAVE-1", "RAVE-2", "OTHER"]


@pytest.mark.asyncio
async def test_lineup_autocomplete_max_25_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog, ctx = _lineup_cog(monkeypatch, [_lineup_event(f"E{i}", i) for i in range(40)])
    assert len(await cog.post_event_autocomplete(make_mock_inter(1), "")) == 25
    ctx.notion_client.list_events = AsyncMock(side_effect=RuntimeError("x"))
    assert await cog.post_event_autocomplete(make_mock_inter(1), "") == []


@pytest.mark.asyncio
async def test_lineup_post_is_admin_gated() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    assert await cog.cog_slash_command_check(make_mock_inter(user_id=2)) is False


# ---------------------------------------------------------------------------
# Command tree
# ---------------------------------------------------------------------------


def _tree_names() -> set[str]:
    from bot.cogs.admin import AdminCog
    from bot.cogs.setup import SetupCog

    names: set[str] = set()
    for cog in (AdminCog(make_mock_ctx(frozenset({1}))), SetupCog(make_mock_ctx(frozenset({1})))):
        for cmd in cog.get_slash_commands():
            children = getattr(cmd, "children", None) or {}
            if not children:
                names.add(cmd.qualified_name)
            for child in children.values():
                grand = getattr(child, "children", None) or {}
                if not grand:
                    names.add(child.qualified_name)
                for g in grand.values():
                    names.add(g.qualified_name)
    return names


def test_command_tree_is_exactly_the_new_set() -> None:
    assert _tree_names() == {
        "setup",
        "config show",
        "create event",
        "post lineup update",
        "book dj slot",
        "cancel dj slot",
        "list all djs",
        "automatic dj booking",
    }


def test_old_commands_are_gone() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    top = {c.name for c in cog.get_slash_commands()}
    for old in ("status", "intervene", "admin", "add-event", "lineup", "list-djs"):
        assert old not in top
    assert "set" not in {c.name for c in cog.config_base.children.values()}
    assert top == {"config", "create", "post", "book", "cancel", "list", "automatic"}


def test_idle_exempt_names_match_the_tree() -> None:
    from bot.cogs.admin import IDLE_EXEMPT_COMMANDS

    assert IDLE_EXEMPT_COMMANDS <= _tree_names()


# ---------------------------------------------------------------------------
# Autocomplete of /book dj slot and /cancel dj slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["book_event_autocomplete", "cancel_event_autocomplete"])
async def test_book_cancel_autocomplete_future_open_events_only(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    events = [
        _lineup_event("RAVE-1", 3),
        _lineup_event("RAVE-DONE", 4, "Done"),
        _lineup_event("RAVE-CANCELED", 5, "Canceled"),
        _lineup_event("RAVE-OLD", -1),
        _lineup_event("RAVE-TODAY", 0),
    ]
    cog, _ = _lineup_cog(monkeypatch, events)
    result = await getattr(cog, name)(make_mock_inter(user_id=1), "rave")
    assert result == ["RAVE-TODAY", "RAVE-1"]


def test_book_and_cancel_event_options_have_autocomplete() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    for cmd in (cog.book_dj_slot, cog.cancel_dj_slot):
        opt = next(o for o in cmd.body.options if o.name == "event")
        assert opt.autocomplete is True


# ---------------------------------------------------------------------------
# /config show
# ---------------------------------------------------------------------------


async def _show(
    monkeypatch: pytest.MonkeyPatch, stored: dict[str, str], active: bool = True
) -> str:
    from bot.cogs.admin import AdminCog

    async def get_value(pool: object, guild_id: int, key: str) -> str | None:
        return stored.get(key)

    monkeypatch.setattr("bot.db.config.get_value", get_value)
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=active))
    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1, guild_id=111)
    await cog.config_show.callback(cog, inter)
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True
    return inter.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_config_show_lists_defaults_and_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _show(monkeypatch, {})
    for label in (
        "DJ channel: not set",
        "Log channel: not set",
        "DJ role: DJ (default)",
        "Invites role: Active Booking (default)",
        "Time zone: Europe/Berlin (default)",
        "Slots per event: 6 (default)",
        "Start time (local): 20:00 (default)",
        "Slot length (minutes): 60 (default)",
        "One event every (days): 7 (default)",
        "Future events kept ready: 6 (default)",
        "Automatic invites: on",
        "(days before an event): 14 (default)",
        "(hours): 12 (default)",
        "Bot status: active",
    ):
        assert label in text
    assert text.endswith("To change a setting, run /setup again.")
    assert text.splitlines()[-1] == "To change a setting, run /setup again."
    assert "outreach_max_contacts" not in text and "recurrence" not in text.lower()
    assert "\u2014" not in text


@pytest.mark.asyncio
async def test_config_show_shows_stored_values(monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _show(
        monkeypatch,
        {
            "dj_channel": "5",
            "log_channel": "6",
            "dj_role_id": "7",
            "invites_role_id": "8",
            "timezone": "Europe/Paris",
            "slots_per_event": "4",
            "default_start_time": "21:30",
            "outreach_enabled": "false",
            "outreach_days_before": "10",
        },
    )
    assert "DJ channel: <#5>" in text
    assert "Log channel: <#6>" in text
    assert "DJ role: <@&7>" in text
    assert "Invites role: <@&8>" in text
    assert "Time zone: Europe/Paris" in text
    assert "Slots per event: 4" in text
    assert "Start time (local): 21:30" in text
    assert "Automatic invites: off" in text
    assert "days before an event): 10" in text


@pytest.mark.asyncio
async def test_config_show_idle_keeps_not_finished_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.ui import texts

    text = await _show(monkeypatch, {}, active=False)
    assert text.startswith(texts.SETUP_NOT_FINISHED)
    assert "Bot status: idle" in text
    assert text.endswith("To change a setting, run /setup again.")


# ---------------------------------------------------------------------------
# Source scans: removals
# ---------------------------------------------------------------------------


def _bot_sources() -> dict[str, str]:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "bot"
    return {str(p): p.read_text() for p in root.rglob("*.py")}


def test_no_reference_to_the_intervention_flow_in_bot() -> None:
    for path, text in _bot_sources().items():
        low = text.lower()
        assert "intervention" not in low, path
        assert "/intervene" not in low, path


def test_no_reference_to_removed_commands_or_settings_in_bot() -> None:
    import re

    commands_re = re.compile(
        r"(?<![\w/.])/(config set|status\b|admin\b|add-event|lineup\b|list-djs)"
    )
    needles = ("set-active", "outreach_max_contacts", "event_recurrence_cron")
    for path, text in _bot_sources().items():
        assert not commands_re.search(text), path
        for needle in needles:
            assert needle not in text, (path, needle)


# ---------------------------------------------------------------------------
# Real disnake can_run on every nesting level
# ---------------------------------------------------------------------------


def _real_commands():
    """Every real command object (all levels) of AdminCog and SetupCog, with its cog set."""
    from bot.cogs.admin import AdminCog
    from bot.cogs.setup import SetupCog

    found = {}

    def walk(cmd, cog):
        cmd.cog = cog  # normally set when the cog is added to the bot
        found[cmd.qualified_name] = cmd
        for child in (getattr(cmd, "children", None) or {}).values():
            walk(child, cog)

    for cog in (AdminCog(make_mock_ctx(frozenset({1}))), SetupCog(make_mock_ctx(frozenset({1})))):
        for cmd in cog.get_slash_commands():
            walk(cmd, cog)
    return found


def _can_run_inter(user_id: int):
    import disnake

    inter = make_mock_inter(user_id=user_id)
    inter.data.type = disnake.ApplicationCommandType.chat_input
    inter.bot.application_command_can_run = AsyncMock(return_value=True)
    return inter


async def _run_chain(chain: list[str], user_id: int, active: bool, monkeypatch) -> tuple:
    """Run can_run for each level like disnake does; returns (allowed, inter)."""
    from disnake.ext import commands as dcommands

    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=active))
    cmds = _real_commands()
    inter = _can_run_inter(user_id)
    for name in chain:
        try:
            ok = await cmds[name].can_run(inter)
        except dcommands.CheckFailure:
            return False, inter
        if not ok:
            return False, inter
    return True, inter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain",
    [["config", "config show"], ["setup"]],
)
async def test_idle_admin_can_run_exempt_commands(monkeypatch, chain) -> None:
    allowed, inter = await _run_chain(chain, 1, False, monkeypatch)
    assert allowed
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain",
    [
        ["book", "book dj", "book dj slot"],
        ["cancel", "cancel dj", "cancel dj slot"],
        ["create", "create event"],
        ["post", "post lineup", "post lineup update"],
        ["list", "list all", "list all djs"],
        ["automatic", "automatic dj", "automatic dj booking"],
    ],
)
async def test_idle_admin_refused_once_for_other_commands(monkeypatch, chain) -> None:
    from bot.ui.gates import ADMIN_IDLE_REPLY

    allowed, inter = await _run_chain(chain, 1, False, monkeypatch)
    assert not allowed
    inter.response.send_message.assert_awaited_once_with(ADMIN_IDLE_REPLY, ephemeral=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain",
    [
        ["config", "config show"],
        ["book", "book dj", "book dj slot"],
        ["create", "create event"],
        ["setup"],
    ],
)
async def test_active_admin_can_run_everything(monkeypatch, chain) -> None:
    allowed, inter = await _run_chain(chain, 1, True, monkeypatch)
    assert allowed
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize(
    "chain", [["config", "config show"], ["book", "book dj", "book dj slot"], ["setup"]]
)
async def test_non_admin_refused_first_without_idle_text(monkeypatch, chain, active) -> None:
    allowed, inter = await _run_chain(chain, 2, active, monkeypatch)
    assert not allowed
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_event_autocomplete_empty_for_non_admin_and_idle(monkeypatch) -> None:
    cog, _ = _lineup_cog(monkeypatch, [_lineup_event("E1", 3)])
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=True))
    assert await cog.post_event_autocomplete(make_mock_inter(user_id=1), "") == ["E1"]
    assert await cog.post_event_autocomplete(make_mock_inter(user_id=2), "") == []
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=False))
    for name in ("post_event_autocomplete", "book_event_autocomplete", "cancel_event_autocomplete"):
        assert await getattr(cog, name)(make_mock_inter(user_id=1), "") == []


@pytest.mark.asyncio
async def test_config_show_in_a_dm_replies_privately() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({123})))
    inter = make_mock_inter(user_id=123)
    inter.guild_id = None
    await cog.config_show.callback(cog, inter)
    inter.response.send_message.assert_awaited_once_with(
        "Please use this command in a server channel.", ephemeral=True
    )


def test_every_admin_command_is_server_only() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({123})))
    names = {"config", "create", "post", "book", "cancel", "automatic", "list"}
    found = {c.name: c for c in cog.get_slash_commands()}
    assert names <= set(found)
    for name in names:
        ctx = found[name].body.contexts
        assert ctx is not None, name
        assert (ctx.guild, ctx.bot_dm, ctx.private_channel) == (True, False, False), name
        assert found[name].body.to_dict()["contexts"] == [0], name


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [123, 999])
async def test_cog_check_in_a_private_message_answers_server_only(user_id: int) -> None:
    from bot.cogs.admin import AdminCog
    from bot.ui import texts

    cog = AdminCog(make_mock_ctx(frozenset({123})))
    inter = make_mock_inter(user_id=user_id)
    inter.guild_id = None
    assert await cog.cog_slash_command_check(inter) is False
    inter.response.send_message.assert_awaited_once_with(texts.ADMIN_SERVER_ONLY, ephemeral=True)
    inter.response.is_done = MagicMock(return_value=True)
    await cog.cog_slash_command_error(inter, commands.CheckFailure())
    inter.response.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# /create event: own shape per event, manual marker
# ---------------------------------------------------------------------------


async def _create(monkeypatch, *, slots=None, start=None, slot_minutes=None, date_str=None):
    """Run /create event with a future date; return (ctx, inter, future_date, init)."""
    import datetime

    from bot.cogs.admin import AdminCog
    from tests.notion.factories import make_event

    future_date = datetime.date.today() + datetime.timedelta(days=2)
    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(
        return_value=make_event(page_id="manual-page", event_id="x", date=future_date)
    )
    _, init = _add_event_patches(monkeypatch)
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    await cog.create_event.callback(
        cog, inter, date_str or future_date.isoformat(), slots, start, slot_minutes
    )
    return ctx, inter, future_date, init


@pytest.mark.asyncio
async def test_create_event_uses_given_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime

    ctx, _, day, init = await _create(monkeypatch, slots=4, start="22:00", slot_minutes=45)
    ctx.notion_client.create_event.assert_awaited_once_with(
        f"{day.isoformat()} Rave Night", day, datetime.time(22, 0), 4, 45
    )
    init.assert_awaited_once_with(ctx.db, "manual-page", 4)


@pytest.mark.asyncio
async def test_create_event_falls_back_to_defaults_per_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime

    ctx, _, day, _ = await _create(monkeypatch, slots=8)
    # defaults of _add_event_patches: start 19:30, slot length 45
    ctx.notion_client.create_event.assert_awaited_once_with(
        f"{day.isoformat()} Rave Night", day, datetime.time(19, 30), 8, 45
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["25:00", "abc", "9"])
async def test_create_event_rejects_bad_start(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    ctx, inter, _, _ = await _create(monkeypatch, start=bad)
    ctx.notion_client.create_event.assert_not_called()
    sent = inter.response.send_message.call_args
    assert "start" in sent.args[0].lower() and sent.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_create_event_rejects_out_of_range_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for kwargs in ({"slots": 0}, {"slots": 101}, {"slot_minutes": 0}, {"slot_minutes": 1441}):
        ctx, inter, _, _ = await _create(monkeypatch, **kwargs)
        ctx.notion_client.create_event.assert_not_called()
        assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_create_event_marks_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    import bot.cogs.admin as admin

    ctx, _, _, _ = await _create(monkeypatch)
    admin.mark_manual.assert_awaited_once_with(ctx.db, "manual-page")


@pytest.mark.asyncio
async def test_create_event_survives_marker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import bot.cogs.admin as admin

    ctx, inter, _, _ = await _create(monkeypatch)  # patches in place
    admin.mark_manual.side_effect = RuntimeError("db down")
    from bot.cogs.admin import AdminCog

    cog = AdminCog(ctx)
    inter2 = make_mock_inter(user_id=1, guild_id=111)
    import datetime

    day = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    await cog.create_event.callback(cog, inter2, day, None, None, None)
    reply, _ = assert_deferred_reply(inter2)
    assert reply.startswith("Event created in Notion")


@pytest.mark.asyncio
async def test_config_show_lists_schedule_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _show(monkeypatch, {})
    assert "Scheduled events: on" in text
    assert "First event date: not set" in text
    off = await _show(
        monkeypatch, {"event_schedule_enabled": "false", "first_event_date": "2026-11-14"}
    )
    assert "Scheduled events: off" in off
    assert "First event date: 2026-11-14" in off


@pytest.mark.asyncio
async def test_create_event_holds_replenish_lock_while_creating_and_marking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime

    import bot.cogs.admin as admin
    from bot.cogs.admin import AdminCog
    from bot.services.event_replenish import _replenish_lock
    from tests.notion.factories import make_event

    seen: dict[str, bool] = {}
    day = datetime.date.today() + datetime.timedelta(days=2)

    async def create(*args, **kwargs):
        seen["create"] = _replenish_lock().locked()
        return make_event(page_id="p", event_id="x", date=day)

    async def mark(*args, **kwargs):
        seen["mark"] = _replenish_lock().locked()

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(side_effect=create)
    _add_event_patches(monkeypatch)
    monkeypatch.setattr(admin, "mark_manual", AsyncMock(side_effect=mark))
    cog = AdminCog(ctx)
    await cog.create_event.callback(
        cog, make_mock_inter(user_id=1, guild_id=111), day.isoformat(), None, None, None
    )
    assert seen == {"create": True, "mark": True}
    assert not _replenish_lock().locked()


@pytest.mark.asyncio
async def test_create_event_tells_organizer_when_marker_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime

    import bot.cogs.admin as admin
    from bot.cogs.admin import AdminCog

    ctx, _, _, _ = await _create(monkeypatch)
    admin.mark_manual.side_effect = RuntimeError("db down")
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    day = (datetime.date.today() + datetime.timedelta(days=4)).isoformat()
    await cog.create_event.callback(cog, inter, day, None, None, None)
    reply, _ = assert_deferred_reply(inter)
    assert reply.startswith("Event created in Notion")
    assert "scheduled" in reply and "could not" in reply.lower()
