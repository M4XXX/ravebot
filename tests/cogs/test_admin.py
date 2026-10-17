"""Tests for bot/cogs/admin.py — AdminCog slash commands and admin check.

[Source: _bmad-output/implementation-artifacts/1-4-admin-channel-configuration-commands.md#Task 4]
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import schedule


@pytest.fixture(autouse=True)
def _schedule_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """The schedule cache refresh reads the config table; these tests use a mocked pool."""
    monkeypatch.setattr("bot.schedule.refresh", AsyncMock(return_value=schedule.DEFAULT_SCHEDULE))


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


# ---------------------------------------------------------------------------
# Story 7.1: /add-event command tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_event_creates_event_and_replies() -> None:
    """add_event calls create_event with correct args and replies with timestamp on success."""
    import datetime

    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(return_value="new-evt-id")

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    future_date = datetime.date.today() + datetime.timedelta(days=7)
    date_str = future_date.isoformat()

    await cog.add_event.callback(cog, inter, date_str)

    ctx.notion_client.create_event.assert_awaited_once()
    call_args = ctx.notion_client.create_event.call_args
    assert call_args[0][1] == future_date  # event_date positional arg
    assert "Rave Night" in call_args[0][0]  # event_name follows naming convention
    reply = inter.response.send_message.call_args[0][0]
    assert "<t:" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True


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
    await cog.add_event.callback(cog, inter, past_date)

    ctx.notion_client.create_event.assert_not_called()
    reply = inter.response.send_message.call_args[0][0].lower()
    assert "invalid date" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_add_event_invalid_format_replies_error() -> None:
    """add_event rejects a non-date string with 'invalid date' reply."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock()

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.add_event.callback(cog, inter, "not-a-date")

    ctx.notion_client.create_event.assert_not_called()
    reply = inter.response.send_message.call_args[0][0].lower()
    assert "invalid date" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True


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
    await cog.add_event.callback(cog, inter, future_date)

    reply = inter.response.send_message.call_args[0][0].lower()
    assert "not ready" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_add_event_notion_write_error_replies_error() -> None:
    """add_event replies 'failed to create' when create_event raises NotionWriteError."""
    import datetime

    from bot.cogs.admin import AdminCog
    from bot.notion.client import NotionWriteError

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.create_event = AsyncMock(side_effect=NotionWriteError("write failed"))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    future_date = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    await cog.add_event.callback(cog, inter, future_date)

    reply = inter.response.send_message.call_args[0][0].lower()
    assert "failed to create" in reply
    assert inter.response.send_message.call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.2: /config set outreach-days-before
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_outreach_days_before_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_outreach_days_before stores 'outreach_days_before' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_outreach_days_before.callback(cog, inter, 10)

    assert set_value_calls == [(111, "outreach_days_before", "10")]
    call_args = inter.response.send_message.call_args
    assert "days" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_outreach_days_before_db_none_replies_not_ready() -> None:
    """config_set_outreach_days_before replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_outreach_days_before.callback(cog, inter, 10)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_outreach_cooldown_hours_db_none_replies_not_ready() -> None:
    """config_set_outreach_cooldown_hours replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_outreach_cooldown_hours.callback(cog, inter, 6)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_lookahead_count_db_none_replies_not_ready() -> None:
    """config_set_lookahead_count replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_lookahead_count.callback(cog, inter, 8)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_outreach_max_db_none_replies_not_ready() -> None:
    """config_set_outreach_max replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_outreach_max.callback(cog, inter, 5)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.2: /config set outreach-cooldown-hours
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_outreach_cooldown_hours_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_outreach_cooldown_hours stores 'outreach_cooldown_hours' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_outreach_cooldown_hours.callback(cog, inter, 6)

    assert set_value_calls == [(111, "outreach_cooldown_hours", "6")]
    call_args = inter.response.send_message.call_args
    assert "hours" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.2: /config set lookahead-count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_lookahead_count_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_lookahead_count stores 'event_lookahead_count' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_lookahead_count.callback(cog, inter, 8)

    assert set_value_calls == [(111, "event_lookahead_count", "8")]
    call_args = inter.response.send_message.call_args
    assert "look-ahead" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.2: /config set outreach-max
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_outreach_max_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_outreach_max stores 'outreach_max_contacts' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_outreach_max.callback(cog, inter, 5)

    assert set_value_calls == [(111, "outreach_max_contacts", "5")]
    call_args = inter.response.send_message.call_args
    assert "max" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.2: /config show
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_show_displays_embed_with_all_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_show reads all config keys and replies with NEBULA_WHITE embed."""
    from bot.cogs.admin import AdminCog
    from bot.ui.embeds import EmbedColors

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    async def mock_get_value(pool: object, guild_id: int, key: str) -> str | None:
        return {"outreach_days_before": "14", "outreach_cooldown_hours": "12"}.get(key)

    monkeypatch.setattr("bot.db.config.get_value", mock_get_value)
    await cog.config_show.callback(cog, inter)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args[1]
    assert call_kwargs.get("ephemeral") is True
    embed = call_kwargs.get("embed")
    assert embed is not None
    assert embed.color.value == EmbedColors.NEBULA_WHITE
    assert len(embed.fields) == 15  # 6 (7.2) + 5 slot (7.3) + timezone, active, dm-log, loops


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


# ---------------------------------------------------------------------------
# Story 7.3: /config set slots-per-event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_slots_per_event_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_slots_per_event stores 'slots_per_event' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_slots_per_event.callback(cog, inter, 4)

    assert set_value_calls == [(111, "slots_per_event", "4")]
    call_args = inter.response.send_message.call_args
    assert "slots" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_slots_per_event_db_none_replies_not_ready() -> None:
    """config_set_slots_per_event replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_slots_per_event.callback(cog, inter, 4)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.3: /config set event-interval-days
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_event_interval_days_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_event_interval_days stores 'event_interval_days' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_event_interval_days.callback(cog, inter, 14)

    assert set_value_calls == [(111, "event_interval_days", "14")]
    call_args = inter.response.send_message.call_args
    assert "days" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_event_interval_days_db_none_replies_not_ready() -> None:
    """config_set_event_interval_days replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_event_interval_days.callback(cog, inter, 14)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.3: /config set slot-duration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_slot_duration_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_slot_duration stores 'slot_duration_minutes' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_slot_duration.callback(cog, inter, 90)

    assert set_value_calls == [(111, "slot_duration_minutes", "90")]
    call_args = inter.response.send_message.call_args
    assert "minutes" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_slot_duration_db_none_replies_not_ready() -> None:
    """config_set_slot_duration replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_slot_duration.callback(cog, inter, 90)

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.3: /config set start-time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_start_time_valid_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_start_time stores 'default_start_time' for valid HH:MM input."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_start_time.callback(cog, inter, "20:00")

    assert set_value_calls == [(111, "default_start_time", "20:00")]
    call_args = inter.response.send_message.call_args
    assert "utc" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_start_time_invalid_format_replies_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_start_time replies error and does NOT call set_value for bad input."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list = []

    async def mock_set_value(*args: object, **kwargs: object) -> None:
        set_value_calls.append(args)

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_start_time.callback(cog, inter, "25:99")

    assert set_value_calls == []  # must NOT store invalid value
    call_args = inter.response.send_message.call_args
    assert "invalid format" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_start_time_db_none_replies_not_ready() -> None:
    """config_set_start_time replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_start_time.callback(cog, inter, "20:00")

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.3: /config set event-schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_event_schedule_stores_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set_event_schedule stores 'event_recurrence_cron' via set_value."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    set_value_calls: list[tuple[int, str, str]] = []

    async def mock_set_value(pool: object, guild_id: int, key: str, value: str) -> None:
        set_value_calls.append((guild_id, key, value))

    monkeypatch.setattr("bot.db.config.set_value", mock_set_value)
    await cog.config_set_event_schedule.callback(cog, inter, "0 20 * * 6")

    assert set_value_calls == [(111, "event_recurrence_cron", "0 20 * * 6")]
    call_args = inter.response.send_message.call_args
    assert "schedule" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_config_set_event_schedule_db_none_replies_not_ready() -> None:
    """config_set_event_schedule replies 'not ready' when db is None."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.db = None
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.config_set_event_schedule.callback(cog, inter, "0 20 * * 6")

    call_args = inter.response.send_message.call_args
    assert "not ready" in call_args[0][0].lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# Story 7.3: config_show field count update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_show_includes_slot_config_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_show embed includes all 11 config fields (6 original + 5 slot config from 7.3)."""
    from bot.cogs.admin import AdminCog
    from bot.ui.embeds import EmbedColors

    ctx = make_mock_ctx(frozenset({1}))
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    async def mock_get_value(pool: object, guild_id: int, key: str) -> str | None:
        return None  # all defaults

    monkeypatch.setattr("bot.db.config.get_value", mock_get_value)
    await cog.config_show.callback(cog, inter)

    call_kwargs = inter.response.send_message.call_args[1]
    assert call_kwargs.get("ephemeral") is True
    embed = call_kwargs.get("embed")
    assert embed is not None
    assert embed.color.value == EmbedColors.NEBULA_WHITE
    assert len(embed.fields) == 15
