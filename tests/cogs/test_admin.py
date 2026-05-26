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


# ---------------------------------------------------------------------------
# Story 6.2: /intervene command tests
# ---------------------------------------------------------------------------


def make_mock_member(user_id: int = 42, display_name: str = "DJ Test") -> MagicMock:
    """Build a minimal mock disnake.Member."""
    member = MagicMock()
    member.id = user_id
    member.display_name = display_name
    member.mention = f"<@{user_id}>"
    return member


@pytest.mark.asyncio
async def test_intervene_start_calls_service_and_replies() -> None:
    """intervene_start calls intervention_service.start and sends ephemeral reply."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.intervention_service = MagicMock()
    ctx.intervention_service.start = AsyncMock()

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=42, display_name="DJ Scope")

    await cog.intervene_start.callback(cog, inter, dj)

    ctx.intervention_service.start.assert_awaited_once_with(42, "DJ Scope")
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "Intervention active" in call_args[0][0]
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_intervene_start_returns_not_ready_when_service_none() -> None:
    """intervene_start replies 'not ready' when intervention_service is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.intervention_service = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member()

    await cog.intervene_start.callback(cog, inter, dj)

    inter.response.send_message.assert_awaited_once()
    assert "not ready" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_intervene_send_calls_service_and_replies() -> None:
    """intervene_send calls send_message and sends ephemeral reply on success."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.intervention_service = MagicMock()
    ctx.intervention_service.send_message = AsyncMock(return_value=True)

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=7, display_name="DJ Wave")

    await cog.intervene_send.callback(cog, inter, dj, "Check your slot!")

    ctx.intervention_service.send_message.assert_awaited_once_with(7, "DJ Wave", "Check your slot!")
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "sent" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_intervene_send_replies_failure_when_dm_blocked() -> None:
    """intervene_send replies with failure message when send_message returns False."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.intervention_service = MagicMock()
    ctx.intervention_service.send_message = AsyncMock(return_value=False)

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=7, display_name="DJ Blocked")

    await cog.intervene_send.callback(cog, inter, dj, "Hello?")

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    reply = call_args[0][0].lower()
    assert "could not" in reply or "blocked" in reply
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_intervene_end_calls_service_and_replies() -> None:
    """intervene_end calls intervention_service.end and sends ephemeral reply."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.intervention_service = MagicMock()
    ctx.intervention_service.end = AsyncMock()

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=55, display_name="DJ Exit")

    await cog.intervene_end.callback(cog, inter, dj)

    ctx.intervention_service.end.assert_awaited_once_with(55, "DJ Exit")
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "ended" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 6.3: /admin book and /admin cancel command tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_book_calls_service_and_replies() -> None:
    """admin_book calls book_slot_override and sends success reply on (True, '')."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.book_slot_override = AsyncMock(return_value=(True, ""))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=42, display_name="DJ Scope")

    await cog.admin_book.callback(cog, inter, dj, "Rave Night #12", 2)

    ctx.admin_booking_service.book_slot_override.assert_awaited_once_with(
        admin_name=inter.author.name,
        dj_id=42,
        dj_name="DJ Scope",
        event_name="Rave Night #12",
        slot_num=2,
    )
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "done" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


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

    await cog.admin_book.callback(cog, inter, dj, "Ghost", 1)

    call_args = inter.response.send_message.call_args
    assert "not found" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_book_returns_not_ready_when_service_none() -> None:
    """admin_book replies 'not ready' when admin_booking_service is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member()

    await cog.admin_book.callback(cog, inter, dj, "Event", 1)

    inter.response.send_message.assert_awaited_once()
    assert "not ready" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_admin_cancel_calls_service_and_replies() -> None:
    """admin_cancel calls cancel_slot_override and sends success reply on (True, '')."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.cancel_slot_override = AsyncMock(return_value=(True, ""))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    dj = make_mock_member(user_id=7, display_name="DJ Wave")

    await cog.admin_cancel.callback(cog, inter, dj, "Rave Night #12", 1)

    ctx.admin_booking_service.cancel_slot_override.assert_awaited_once_with(
        admin_name=inter.author.name,
        dj_id=7,
        dj_name="DJ Wave",
        event_name="Rave Night #12",
        slot_num=1,
    )
    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    assert "done" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


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

    await cog.admin_cancel.callback(cog, inter, dj, "Rave Night #12", 1)

    call_args = inter.response.send_message.call_args
    assert "no booking found" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 6.4: /admin dj set-active command tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_dj_set_active_true_calls_service_and_replies() -> None:
    """admin_dj_set_active with active=True calls set_dj_active_status and replies."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.set_dj_active_status = AsyncMock(return_value=(True, ""))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member(user_id=42, display_name="DJ Scope")

    await cog.admin_dj_set_active.callback(cog, inter, dj, True)

    ctx.admin_booking_service.set_dj_active_status.assert_awaited_once_with(
        111, 42, "DJ Scope", True
    )
    call_args = inter.response.send_message.call_args
    assert "active" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_dj_set_active_false_calls_service_and_replies() -> None:
    """admin_dj_set_active with active=False calls set_dj_active_status and replies."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.set_dj_active_status = AsyncMock(return_value=(True, ""))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member(user_id=42, display_name="DJ Exit")

    await cog.admin_dj_set_active.callback(cog, inter, dj, False)

    ctx.admin_booking_service.set_dj_active_status.assert_awaited_once_with(
        111, 42, "DJ Exit", False
    )
    call_args = inter.response.send_message.call_args
    assert "removed" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_dj_set_active_dj_not_registered_replies_with_error() -> None:
    """admin_dj_set_active relays 'not registered' error from service."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = MagicMock()
    ctx.admin_booking_service.set_dj_active_status = AsyncMock(
        return_value=(False, "@DJ Ghost is not registered in the system.")
    )

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member(user_id=99, display_name="DJ Ghost")

    await cog.admin_dj_set_active.callback(cog, inter, dj, True)

    call_args = inter.response.send_message.call_args
    assert "not registered" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_dj_set_active_not_ready_when_service_none() -> None:
    """admin_dj_set_active replies 'not ready' when admin_booking_service is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.admin_booking_service = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)
    dj = make_mock_member()

    await cog.admin_dj_set_active.callback(cog, inter, dj, True)

    inter.response.send_message.assert_awaited_once()
    assert "not ready" in inter.response.send_message.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# Story 6.5: /status and /list-djs command tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_event_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """status_cmd builds a MISSION_GOLD embed for the next upcoming event."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(return_value=[{
        "id": "evt-001",
        "properties": {
            "Name": {"title": [{"plain_text": "Rave Night #12"}]},
            "Date": {"date": {"start": "2026-06-14T20:00:00+00:00"}},
        },
    }])
    monkeypatch.setattr(
        "bot.cogs.admin.get_booked_djs_for_event",
        AsyncMock(return_value=[
            {"slot_index": 0, "dj_name": "DJ Scope", "discord_user_id": 42, "event_id": "evt-001"}
        ]),
    )
    monkeypatch.setattr("bot.cogs.admin.db_config.get_value", AsyncMock(return_value="6"))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.status_cmd.callback(cog, inter)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args[1]
    assert call_kwargs.get("ephemeral") is True
    embed = call_kwargs.get("embed")
    assert embed is not None
    assert "Rave Night #12" in embed.title


@pytest.mark.asyncio
async def test_status_no_events_replies_message() -> None:
    """status_cmd replies with plain text when Notion has no upcoming events."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(return_value=[])

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.status_cmd.callback(cog, inter)

    call_args = inter.response.send_message.call_args
    assert "no upcoming events" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_status_not_ready_when_notion_none() -> None:
    """status_cmd replies 'not ready' when notion_client is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.status_cmd.callback(cog, inter)

    assert "not ready" in inter.response.send_message.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_status_not_ready_when_db_none() -> None:
    """status_cmd replies 'not ready' when db is None (notion_client set, db absent)."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.db = None

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.status_cmd.callback(cog, inter)

    assert "not ready" in inter.response.send_message.call_args[0][0].lower()


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

    await cog.list_djs.callback(cog, inter)

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

    await cog.list_djs.callback(cog, inter)

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

    await cog.list_djs.callback(cog, inter)

    assert "not ready" in inter.response.send_message.call_args[0][0].lower()
