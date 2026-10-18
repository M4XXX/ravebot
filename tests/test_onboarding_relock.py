"""A closed onboarding form never locks the DJ out of [Go Active], and the way out is mirrored."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.context import BotContext


def _ctx(dj_row: dict | None) -> BotContext:
    ctx = BotContext(config=MagicMock())
    ctx.db = MagicMock()
    ctx.db.fetchrow = AsyncMock(return_value=dj_row)
    ctx.log_service = AsyncMock()
    return ctx


def _inter(user_id: int = 7) -> MagicMock:
    inter = MagicMock()
    inter.author.id = user_id
    inter.author.name = "dj"
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()
    inter.response.send_modal = AsyncMock()
    return inter


@pytest.mark.asyncio
@pytest.mark.parametrize("dj_row", [None, {"onboarding_complete": False}])
async def test_go_active_takes_over_the_lock_of_a_closed_form(dj_row: dict | None) -> None:
    from bot.ui.views import OnboardingIntroView, OnboardingModal

    ctx = _ctx(dj_row)
    lock = ctx.get_user_lock(7)
    await lock.acquire()  # first [Go Active]: the form opened, then the DJ closed it
    closed_form = OnboardingModal(ctx, 7, lock, 42)
    inter = _inter()

    await OnboardingIntroView(ctx, 7, 42).go_active.callback(inter)

    inter.response.send_modal.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()
    assert lock.locked() is True  # held again, by the new form
    new_form = inter.response.send_modal.call_args[0][0]
    assert new_form._lock_generation == lock.generation != closed_form._lock_generation
    await closed_form.on_timeout()  # the old form timing out later must not free the new one
    assert lock.locked() is True


@pytest.mark.asyncio
async def test_registered_dj_with_open_flow_gets_start_over_not_a_form() -> None:
    from bot.ui.views import FLOW_OPEN_TEXT, OnboardingIntroView, StartOverView

    ctx = _ctx({"onboarding_complete": True})
    lock = ctx.get_user_lock(7)
    await lock.acquire()  # e.g. mid-booking
    generation = lock.generation
    inter = _inter()

    await OnboardingIntroView(ctx, 7, 42).go_active.callback(inter)

    inter.response.send_modal.assert_not_awaited()
    args, kwargs = inter.response.send_message.call_args
    assert args[0] == FLOW_OPEN_TEXT
    assert isinstance(kwargs["view"], StartOverView) and kwargs["ephemeral"] is True
    assert lock.locked() is True and lock.generation == generation  # booking flow untouched


@pytest.mark.asyncio
async def test_go_active_without_open_flow_opens_the_form() -> None:
    from bot.ui.views import OnboardingIntroView

    ctx = _ctx(None)
    inter = _inter()
    await OnboardingIntroView(ctx, 7, 42).go_active.callback(inter)
    inter.response.send_modal.assert_awaited_once()
    assert ctx.get_user_lock(7).locked() is True


@pytest.mark.asyncio
async def test_try_again_also_takes_over_a_closed_form() -> None:
    from bot.ui.views import RetryOnboardingModalView

    ctx = _ctx(None)
    lock = ctx.get_user_lock(7)
    await lock.acquire()
    inter = _inter()
    await RetryOnboardingModalView(ctx, 7, 42).retry.callback(inter)
    inter.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_go_active_without_profile_starts_onboarding_for_dj_role_holder() -> None:
    from bot.ui.views import FallbackMenuView, OnboardingIntroView, onboarding_intro_text

    ctx = _ctx(None)
    role = MagicMock()
    role.name = "DJ"
    member = MagicMock(roles=[role])
    guild = MagicMock(id=42)
    guild.get_member = MagicMock(return_value=member)
    inter = _inter()
    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=False)), patch(
        "bot.ui.views.served.served_guild", return_value=guild
    ):
        await FallbackMenuView(ctx).go_active.callback(inter)

    args, kwargs = inter.response.send_message.call_args
    assert args[0] == onboarding_intro_text()
    assert isinstance(kwargs["view"], OnboardingIntroView)


@pytest.mark.asyncio
async def test_menu_go_active_without_dj_role_gets_the_role_notice() -> None:
    from bot.ui.views import NO_DJ_ROLE_TEXT, FallbackMenuView

    ctx = _ctx(None)
    guild = MagicMock(id=42)
    guild.get_member = MagicMock(return_value=MagicMock(roles=[]))
    inter = _inter()
    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=False)), patch(
        "bot.ui.views.served.served_guild", return_value=guild
    ):
        await FallbackMenuView(ctx).go_active.callback(inter)

    assert inter.response.send_message.call_args[0][0] == NO_DJ_ROLE_TEXT


@pytest.mark.asyncio
async def test_start_over_is_mirrored_to_the_transcript() -> None:
    from bot.ui.views import StartOverView

    ctx = _ctx(None)
    inter = _inter()
    await StartOverView(ctx).start_over.callback(inter)  # disnake binds the button itself
    directions = [c.kwargs["direction"] for c in ctx.log_service.post_dm_transcript.await_args_list]
    assert directions == ["DJ", "BOT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dj_row,roles,expected", [
    ({"onboarding_complete": True}, [], "NO_DJ_ROLE_TEXT"),   # profile, role taken away
    (None, [], "NOT_REGISTERED_TEXT"),                       # never onboarded
    ({"onboarding_complete": False}, ["DJ"], "NOT_REGISTERED_TEXT"),
])
async def test_booking_refusal_says_what_is_missing(dj_row, roles, expected) -> None:
    from bot import ui

    ctx = _ctx(dj_row)
    role_mocks = []
    for name in roles:
        role = MagicMock()
        role.name = name
        role_mocks.append(role)
    guild = MagicMock(id=42)
    guild.get_member = MagicMock(return_value=MagicMock(roles=role_mocks))
    with patch("bot.ui.views.served.served_guild", return_value=guild):
        text = await ui.views.not_registered_text(ctx, _inter())
    assert text == getattr(ui.views, expected)
