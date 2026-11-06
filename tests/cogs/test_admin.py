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
    """Build a minimal mock ApplicationCommandInteraction.

    ``inter.order`` records "defer", "followup" and "send_message" in call order.
    """
    inter = MagicMock()
    inter.author.id = user_id
    inter.guild_id = guild_id
    inter.response = AsyncMock()
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
    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1)
    ctx.admin_booking_service.book_slot_override = AsyncMock(
        return_value=(True, ""), side_effect=lambda **k: inter.order.append("service") or (True, "")
    )
    dj = make_mock_member(user_id=42, display_name="DJ Scope")

    await cog.admin_book.callback(cog, inter, dj, "Rave Night #12", 2)

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

    await cog.admin_book.callback(cog, inter, dj, "Ghost", 1)

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

    await cog.admin_book.callback(cog, inter, dj, "Event", 1)

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

    await cog.admin_cancel.callback(cog, inter, dj, "Rave Night #12", 1)

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

    await cog.admin_cancel.callback(cog, inter, dj, "Rave Night #12", 1)

    reply, _ = assert_deferred_reply(inter)
    assert "no booking found" in reply.lower()


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


def _status_event():
    import datetime

    from tests.notion.factories import make_event, make_slot

    return make_event(
        event_id="2026-06-14 Rave Night #12",
        page_id="evt-001",
        date=datetime.date(2026, 6, 14),
        start=datetime.time(20, 0),
        slot_minutes=30,
        slot_count=4,
        slots=[
            make_slot(1, dj_page_id="dj-a", dj_name="DJ Scope", discord_id=42),
            make_slot(2),
            make_slot(3, dj_page_id="dj-a", dj_name="DJ Scope", discord_id=42),
            make_slot(4, dj_page_id="dj-b", dj_name="DJ Wave", discord_id=99),
        ],
    )


@pytest.mark.asyncio
async def test_status_returns_event_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """status_cmd builds a MISSION_GOLD embed for the next event, counts from slot rows."""
    import datetime

    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[_status_event()])
    # Config says 6 slots; the slot rows (4) must win
    monkeypatch.setattr("bot.cogs.admin.db_config.get_value", AsyncMock(return_value="6"))

    cog = AdminCog(ctx)
    inter = make_mock_inter(user_id=1, guild_id=111)

    await cog.status_cmd.callback(cog, inter)

    ctx.notion_client.list_events.assert_awaited_once_with()
    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args[1]
    assert call_kwargs.get("ephemeral") is True
    embed = call_kwargs.get("embed")
    assert embed is not None
    assert "2026-06-14 Rave Night #12" in embed.title
    fields = {f.name: f.value for f in embed.fields}
    start = datetime.datetime(2026, 6, 14, 20, 0, tzinfo=datetime.UTC)
    assert fields["Event Date"] == f"<t:{int(start.timestamp())}:f>"
    # Slot rows decide: 4 slots, 3 booked (DJ Scope holds two), 1 open
    assert fields["Total Slots"] == "4"
    assert fields["Booked"] == "3"
    assert fields["Open"] == "1"
    slot2 = int((start + datetime.timedelta(minutes=30)).timestamp())
    assert fields[f"Slot 2 · <t:{slot2}:f>"] == "Open"
    slot3 = int((start + datetime.timedelta(minutes=60)).timestamp())
    assert fields[f"Slot 3 · <t:{slot3}:f>"] == "DJ Scope"
    slot4 = int((start + datetime.timedelta(minutes=90)).timestamp())
    assert fields[f"Slot 4 · <t:{slot4}:f>"] == "DJ Wave"


@pytest.mark.asyncio
async def test_status_no_events_replies_message() -> None:
    """status_cmd replies with plain text when Notion has no upcoming events."""
    from bot.cogs.admin import AdminCog

    ctx = make_mock_ctx(frozenset({1}))
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[])

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


def _add_event_patches(monkeypatch: pytest.MonkeyPatch, shape=None):
    """Patch default_event_shape and init_event_slots in the admin cog; return (shape, init)."""
    import datetime

    from bot.services.event_defaults import EventShape

    shape = shape or EventShape(start=datetime.time(19, 30), slot_minutes=45, slot_count=5)
    shape_mock = AsyncMock(return_value=shape)
    init = AsyncMock()
    monkeypatch.setattr("bot.cogs.admin.default_event_shape", shape_mock)
    monkeypatch.setattr("bot.cogs.admin.init_event_slots", init)
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

    await cog.add_event.callback(cog, inter, date_str)
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
    assert reply == f"Event created in Notion for <t:{int(created.slot_start(0).timestamp())}:f>."


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
    await cog.add_event.callback(cog, inter, future_date)

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
    await cog.add_event.callback(cog, inter, future_date)

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
    await cog.add_event.callback(cog, inter, past_date)

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

    await cog.add_event.callback(cog, inter, "not-a-date")

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
    await cog.add_event.callback(cog, inter, future_date)

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
    await cog.add_event.callback(cog, inter, future_date)

    reply, _ = assert_deferred_reply(inter)
    assert "failed to create" in reply.lower()


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
    assert len(embed.fields) == 12  # 11 config keys + Automatic outreach


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
    assert len(embed.fields) == 12


@pytest.mark.asyncio
async def test_cog_error_after_defer_sends_followup() -> None:
    """An unexpected error after defer is answered via followup, not send_message."""
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1)
    inter.response.is_done = MagicMock(return_value=True)
    await cog.cog_slash_command_error(inter, RuntimeError("boom"))
    inter.followup.send.assert_awaited_once_with(
        "Something went wrong — please try again.", ephemeral=True
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
        "Something went wrong — please try again.", ephemeral=True
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
# /lineup post
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
    await cog.lineup_post.callback(cog, inter, None)
    ctx.lineup_service.post_event.assert_awaited_once_with(first)
    assert assert_deferred_reply(inter)[0] == "Lineup posted."


@pytest.mark.asyncio
async def test_lineup_post_specific_event_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _lineup_event("E1", 3), _lineup_event("E2", 10)
    cog, ctx = _lineup_cog(monkeypatch, [first, second])
    inter = make_mock_inter(user_id=1)
    await cog.lineup_post.callback(cog, inter, "e2")
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
    await cog.lineup_post.callback(cog, inter, event)
    ctx.lineup_service.post_event.assert_not_awaited()
    text = assert_deferred_reply(inter)[0]
    assert text == "That event is not available. Pick one from the list."


@pytest.mark.asyncio
async def test_lineup_post_no_future_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.ui import texts

    cog, ctx = _lineup_cog(monkeypatch, [_lineup_event("PAST1", -2)])
    inter = make_mock_inter(user_id=1)
    await cog.lineup_post.callback(cog, inter, None)
    ctx.lineup_service.post_event.assert_not_awaited()
    assert assert_deferred_reply(inter)[0] == texts.NO_LINEUP


@pytest.mark.asyncio
async def test_lineup_post_failure_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    cog, _ = _lineup_cog(monkeypatch, [_lineup_event("E1", 3)], post_ok=False)
    inter = make_mock_inter(user_id=1)
    await cog.lineup_post.callback(cog, inter, None)
    text = assert_deferred_reply(inter)[0]
    assert "—" not in text
    assert text != "Lineup posted."


@pytest.mark.asyncio
async def test_lineup_post_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    cog, ctx = _lineup_cog(monkeypatch, [])
    ctx.lineup_service = None
    inter = make_mock_inter(user_id=1)
    await cog.lineup_post.callback(cog, inter, None)
    text = inter.response.send_message.call_args[0][0]
    assert "not ready" in text.lower() and "—" not in text


@pytest.mark.asyncio
async def test_lineup_post_notion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cog, ctx = _lineup_cog(monkeypatch, [])
    ctx.notion_client.list_events = AsyncMock(side_effect=RuntimeError("boom"))
    inter = make_mock_inter(user_id=1)
    await cog.lineup_post.callback(cog, inter, None)
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
    assert await cog.lineup_post_autocomplete(inter, "rave") == ["RAVE-1", "RAVE-2"]
    assert await cog.lineup_post_autocomplete(inter, "") == ["RAVE-1", "RAVE-2", "OTHER"]


@pytest.mark.asyncio
async def test_lineup_autocomplete_max_25_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog, ctx = _lineup_cog(monkeypatch, [_lineup_event(f"E{i}", i) for i in range(40)])
    assert len(await cog.lineup_post_autocomplete(make_mock_inter(1), "")) == 25
    ctx.notion_client.list_events = AsyncMock(side_effect=RuntimeError("x"))
    assert await cog.lineup_post_autocomplete(make_mock_inter(1), "") == []


def test_lineup_post_is_admin_gated() -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    assert cog.cog_slash_command_check(make_mock_inter(user_id=2)) is False


# ---------------------------------------------------------------------------
# Global outreach switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "stored", "reply"),
    [
        (True, "true", "Automatic outreach is on."),
        (False, "false", "Automatic outreach is off. Booking, reminders and DMs keep working."),
    ],
)
async def test_config_set_outreach_stores_value_and_replies_privately(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, stored: str, reply: str
) -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1, guild_id=111)
    set_value = AsyncMock()
    monkeypatch.setattr("bot.db.config.set_value", set_value)

    await cog.config_set_outreach.callback(cog, inter, enabled)

    set_value.assert_awaited_once_with(cog.ctx.db, 111, "outreach_enabled", stored)
    inter.response.send_message.assert_awaited_once_with(reply, ephemeral=True)
    assert "\u2014" not in reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "shown"), [(None, "on"), ("true", "on"), ("false", "off")]
)
async def test_config_show_lists_automatic_outreach_row(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, shown: str
) -> None:
    from bot.cogs.admin import AdminCog

    cog = AdminCog(make_mock_ctx(frozenset({1})))
    inter = make_mock_inter(user_id=1, guild_id=111)

    async def mock_get_value(pool: object, guild_id: int, key: str) -> str | None:
        return raw if key == "outreach_enabled" else None

    monkeypatch.setattr("bot.db.config.get_value", mock_get_value)
    await cog.config_show.callback(cog, inter)

    embed = inter.response.send_message.call_args[1]["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Automatic outreach"] == shown
