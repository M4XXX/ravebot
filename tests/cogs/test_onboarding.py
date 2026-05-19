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

    cog = OnboardingCog(make_ctx())
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

    cog = OnboardingCog(make_ctx())
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
    cog = OnboardingCog(ctx)
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
    cog = OnboardingCog(ctx)
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
    cog = OnboardingCog(ctx)
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
    view = OnboardingIntroView(ctx, 12345)

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
    view = OnboardingIntroView(ctx, 12345)

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
