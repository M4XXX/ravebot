"""Tests for bot/cogs/onboarding.py — DJ role detection and DM initiation."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.ui.embeds import ErrorCode
from tests.conftest import make_real_ctx


def make_role(name: str) -> MagicMock:
    role = MagicMock(spec=disnake.Role)
    role.name = name
    role.__hash__ = lambda self: hash(name)
    role.__eq__ = lambda self, other: self.name == getattr(other, "name", None)
    return role


def make_member(user_id: int = 12345, name: str = "TestDJ") -> MagicMock:
    member = MagicMock(spec=disnake.Member)
    member.id = user_id
    member.name = name
    member.send = AsyncMock()
    return member


def make_ctx(db: object = None, log: object = None) -> MagicMock:
    ctx = MagicMock()
    ctx.db = db
    ctx.log_service = log
    return ctx


@pytest.mark.asyncio
async def test_on_member_update_ignores_no_new_roles() -> None:
    """No DM sent when the member update has no new roles added."""
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(make_ctx(), MagicMock())
    role = make_role("DJ")
    before = make_member()
    before.roles = [role]
    after = make_member()
    after.roles = [role]  # same roles — no change

    await cog.on_member_update(before, after)

    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_update_ignores_non_dj_role() -> None:
    """No DM sent when a non-DJ role is newly assigned."""
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(make_ctx(), MagicMock())
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("Moderator")]

    await cog.on_member_update(before, after)

    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_update_skips_existing_dj() -> None:
    """No DM sent when the DJ already exists in the database."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_ctx(db=MagicMock())
    cog = OnboardingCog(ctx, MagicMock())
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("DJ")]

    existing_record = MagicMock()
    with patch(
        "bot.cogs.onboarding.get_dj_by_discord_id",
        new=AsyncMock(return_value=existing_record),
    ):
        await cog.on_member_update(before, after)

    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_update_sends_dm_to_new_dj() -> None:
    """DM sent with correct message text when a new DJ role is detected."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_ctx(db=MagicMock())
    cog = OnboardingCog(ctx, MagicMock())
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("DJ")]

    with patch(
        "bot.cogs.onboarding.get_dj_by_discord_id",
        new=AsyncMock(return_value=None),
    ):
        await cog.on_member_update(before, after)

    after.send.assert_awaited_once()
    sent_content = after.send.call_args[0][0]
    assert "RaveBot" in sent_content
    assert "booking pool" in sent_content


@pytest.mark.asyncio
async def test_on_member_update_posts_error_on_dm_failure() -> None:
    """ERR_DM_FAILED posted to admin log when DM is blocked; no exception raised."""
    from bot.cogs.onboarding import OnboardingCog

    log = MagicMock()
    log.post_error = AsyncMock()
    ctx = make_ctx(db=MagicMock(), log=log)
    cog = OnboardingCog(ctx, MagicMock())
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("DJ")]
    after.send = AsyncMock(
        side_effect=disnake.Forbidden(MagicMock(), "Cannot send messages to this user")
    )

    with patch(
        "bot.cogs.onboarding.get_dj_by_discord_id",
        new=AsyncMock(return_value=None),
    ):
        await cog.on_member_update(before, after)  # must not raise

    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_DM_FAILED


# ---------------------------------------------------------------------------
# Story 2.2: OnboardingIntroView.go_active button handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_go_active_sends_modal() -> None:
    """go_active acquires lock and sends OnboardingModal when no flow is active."""
    from bot.ui.views import OnboardingIntroView

    ctx = make_real_ctx(db=MagicMock())
    view = OnboardingIntroView(ctx, 12345, guild_id=99999)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 12345
    inter.response.send_modal = AsyncMock()
    inter.response.send_message = AsyncMock()

    # disnake View.__init__ sets view.go_active to a Button with callback=partial(fn, view, btn)
    # So call it as: await view.go_active.callback(inter)
    await view.go_active.callback(inter)

    inter.response.send_modal.assert_awaited_once()
    inter.response.send_message.assert_not_called()
    # Lock should be held after go_active (released by modal callback)
    assert ctx.get_user_lock(12345).locked()


@pytest.mark.asyncio
async def test_go_active_concurrent_guard() -> None:
    """go_active sends ephemeral message and skips modal when lock is already held."""
    from bot.ui.views import OnboardingIntroView

    ctx = make_real_ctx(db=MagicMock())
    view = OnboardingIntroView(ctx, 12345, guild_id=99999)

    # Pre-acquire the lock to simulate an active flow
    await ctx.get_user_lock(12345).acquire()

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 12345
    inter.response.send_modal = AsyncMock()
    inter.response.send_message = AsyncMock()

    await view.go_active.callback(inter)

    inter.response.send_modal.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    sent_content = inter.response.send_message.call_args[0][0]
    assert "flow" in sent_content.lower() or "open" in sent_content.lower()


# ---------------------------------------------------------------------------
# Story 2.3: guild_id threading
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_member_update_passes_guild_id_to_intro_view() -> None:
    """on_member_update passes after.guild.id to OnboardingIntroView."""
    from bot.cogs.onboarding import OnboardingCog
    from bot.ui.views import OnboardingIntroView

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx, MagicMock())

    dj_role = make_role("DJ")
    before = make_member()
    before.roles = []
    after = make_member(user_id=55555)
    after.roles = [dj_role]
    after.guild.id = 88888

    with patch(
        "bot.cogs.onboarding.get_dj_by_discord_id",
        new=AsyncMock(return_value=None),
    ):
        await cog.on_member_update(before, after)

    after.send.assert_awaited_once()
    send_kwargs = after.send.call_args.kwargs
    view = send_kwargs.get("view")
    assert isinstance(view, OnboardingIntroView)
    assert view.guild_id == 88888


# ---------------------------------------------------------------------------
# Story 2.4: on_message + FallbackMenuView trigger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_message_dm_sends_fallback_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Free-text DM outside active flow sends FallbackMenuView."""
    from bot.cogs.onboarding import OnboardingCog
    from bot.ui.views import FallbackMenuView

    monkeypatch.setattr(
        "bot.cogs.onboarding.is_under_intervention",
        AsyncMock(return_value=False),
    )

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx, MagicMock())

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 11111
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_awaited_once()
    send_kwargs = message.channel.send.call_args.kwargs
    assert isinstance(send_kwargs.get("view"), FallbackMenuView)


@pytest.mark.asyncio
async def test_on_message_guild_message_ignored() -> None:
    """Messages in guild channels do not trigger FallbackMenuView."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx, MagicMock())

    message = MagicMock(spec=disnake.Message)
    message.guild = MagicMock()  # guild message
    message.author.id = 22222
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_bot_message_ignored() -> None:
    """Bot's own DMs do not trigger FallbackMenuView."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx, MagicMock())

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 33333
    message.author.bot = True
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_skips_during_active_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """FallbackMenuView NOT shown when user has an active flow (lock held)."""
    from bot.cogs.onboarding import OnboardingCog

    monkeypatch.setattr(
        "bot.cogs.onboarding.is_under_intervention",
        AsyncMock(return_value=False),
    )

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx, MagicMock())

    await ctx.get_user_lock(44444).acquire()

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 44444
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_dm_logs_transcript_for_incoming_and_outgoing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 6.1 AC2 + AC1: incoming DM and fallback menu response are both transcribed."""
    from bot.cogs.onboarding import OnboardingCog

    monkeypatch.setattr(
        "bot.cogs.onboarding.is_under_intervention",
        AsyncMock(return_value=False),
    )

    log_service = MagicMock()
    log_service.post_dm_transcript = AsyncMock()
    ctx = make_real_ctx(db=MagicMock())
    ctx.log_service = log_service

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 55555
    message.author.name = "DJ Tester"
    message.author.bot = False
    message.content = "hello bot"
    message.channel.send = AsyncMock()

    cog = OnboardingCog(ctx, MagicMock())
    await cog.on_message(message)

    # AC2: incoming message logged first; AC1: outgoing fallback menu logged second
    calls = log_service.post_dm_transcript.call_args_list
    assert len(calls) == 2
    incoming = calls[0].kwargs
    assert incoming["direction"] == "DJ"
    assert incoming["dj_name"] == "DJ Tester"
    assert incoming["content"] == "hello bot"
    outgoing = calls[1].kwargs
    assert outgoing["direction"] == "BOT"
    assert "buttons" in outgoing["content"].lower()


# ---------------------------------------------------------------------------
# Story 6.2: intervention check in on_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_under_intervention_forwards_to_admin_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 6.2 AC3: DJ under intervention → forward to admin log, no FallbackMenuView."""
    from bot.cogs.onboarding import OnboardingCog

    monkeypatch.setattr(
        "bot.cogs.onboarding.is_under_intervention",
        AsyncMock(return_value=True),
    )

    log_service = MagicMock()
    log_service.post_dm_transcript = AsyncMock()
    log_service.post_intervention = AsyncMock()

    ctx = make_real_ctx(db=MagicMock())
    ctx.log_service = log_service

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 99
    message.author.name = "DJ Blocked"
    message.author.bot = False
    message.content = "I need help"
    message.channel.send = AsyncMock()

    cog = OnboardingCog(ctx, MagicMock())
    await cog.on_message(message)

    # Transcript was still logged (6.1 hook fires before intervention check)
    log_service.post_dm_transcript.assert_awaited_once()

    # post_intervention called to forward to admin
    log_service.post_intervention.assert_awaited_once()
    call = log_service.post_intervention.call_args
    assert call.kwargs["sender"] == "@DJ Blocked"
    assert call.kwargs["recipient"] == "ADMIN"
    assert call.kwargs["content"] == "I need help"

    # No FallbackMenuView sent
    message.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_under_intervention_suppresses_even_when_log_service_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 6.2 HIGH fix: intervention suppression fires even when log_service is None."""
    from bot.cogs.onboarding import OnboardingCog

    monkeypatch.setattr(
        "bot.cogs.onboarding.is_under_intervention",
        AsyncMock(return_value=True),
    )

    ctx = make_real_ctx(db=MagicMock())
    ctx.log_service = None  # log_service absent — suppression must still work

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 88
    message.author.name = "DJ Silent"
    message.author.bot = False
    message.content = "any message"
    message.channel.send = AsyncMock()

    cog = OnboardingCog(ctx, MagicMock())
    await cog.on_message(message)

    # No FallbackMenuView despite log_service being None
    message.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_not_under_intervention_shows_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DJ NOT under intervention → normal FallbackMenuView shown."""
    from bot.cogs.onboarding import OnboardingCog
    from bot.ui.views import FallbackMenuView

    monkeypatch.setattr(
        "bot.cogs.onboarding.is_under_intervention",
        AsyncMock(return_value=False),
    )

    ctx = make_real_ctx(db=MagicMock())

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 77
    message.author.name = "DJ Free"
    message.author.bot = False
    message.content = "hello"
    message.channel.send = AsyncMock()

    cog = OnboardingCog(ctx, MagicMock())
    await cog.on_message(message)

    message.channel.send.assert_awaited_once()
    view = message.channel.send.call_args.kwargs.get("view")
    assert isinstance(view, FallbackMenuView)
