"""A DJ never stays locked out: abandoned or failed flows free the per-user lock."""
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.context import STALE_FLOW_SECONDS, FlowLock


@pytest.mark.asyncio
async def test_abandoned_lock_frees_itself() -> None:
    lock = FlowLock()
    with patch("bot.context.time.monotonic", return_value=1000.0):
        await lock.acquire()
    with patch("bot.context.time.monotonic", return_value=1000.0 + STALE_FLOW_SECONDS - 1):
        assert lock.locked() is True
    with patch("bot.context.time.monotonic", return_value=1000.0 + STALE_FLOW_SECONDS + 1):
        assert lock.locked() is False
    await lock.acquire()  # does not block
    assert lock.locked() is True


def test_release_if_held_is_safe_when_free() -> None:
    lock = FlowLock()
    lock.release_if_held()
    assert lock.locked() is False


def _modal(lock: FlowLock):
    from bot.ui.views import OnboardingModal

    return OnboardingModal(MagicMock(), user_id=7, lock=lock, guild_id=42)


@pytest.mark.asyncio
async def test_closing_the_onboarding_form_frees_the_lock() -> None:
    lock = FlowLock()
    await lock.acquire()
    await _modal(lock).on_timeout()
    assert lock.locked() is False


@pytest.mark.asyncio
async def test_failed_onboarding_submission_frees_the_lock() -> None:
    lock = FlowLock()
    await lock.acquire()
    inter = MagicMock()
    inter.response.send_message = AsyncMock()
    await _modal(lock).on_error(RuntimeError("db down"), inter)
    assert lock.locked() is False
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_button_frees_the_users_lock() -> None:
    from bot.context import BotContext
    from bot.ui.views import FLOW_FAILED_TEXT, FallbackMenuView

    ctx = BotContext(config=MagicMock())
    lock = ctx.get_user_lock(7)
    inter = MagicMock()
    inter.author.id = 7
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()
    view = FallbackMenuView(ctx)
    assert await view.interaction_check(inter) is True  # no flow open when the click arrives
    await lock.acquire()  # the handler starts a flow, then fails
    await view.on_error(RuntimeError("boom"), MagicMock(), inter)
    assert lock.locked() is False
    inter.response.send_message.assert_awaited_once_with(FLOW_FAILED_TEXT, ephemeral=True)


@pytest.mark.asyncio
async def test_failed_button_keeps_the_lock_of_a_flow_that_was_already_open() -> None:
    from bot.context import BotContext
    from bot.ui.views import FallbackMenuView

    ctx = BotContext(config=MagicMock())
    lock = ctx.get_user_lock(7)
    await lock.acquire()  # e.g. a booking flow in progress
    inter = MagicMock()
    inter.author.id = 7
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()
    view = FallbackMenuView(ctx)
    await view.interaction_check(inter)
    await view.on_error(RuntimeError("db hiccup"), MagicMock(), inter)
    assert lock.locked() is True


@pytest.mark.asyncio
async def test_closed_form_cannot_free_the_lock_of_a_later_flow() -> None:
    lock = FlowLock()
    await lock.acquire()
    modal = _modal(lock)
    lock.release()  # the DJ closed the form and pressed Start over
    await lock.acquire()  # ...and started booking
    await modal.on_timeout()
    assert lock.locked() is True


@pytest.mark.asyncio
async def test_start_over_frees_the_lock_and_shows_the_menu() -> None:
    from bot.context import BotContext
    from bot.ui.views import FallbackMenuView, StartOverView

    ctx = BotContext(config=MagicMock())
    lock = ctx.get_user_lock(7)
    await lock.acquire()
    inter = MagicMock()
    inter.author.id = 7
    inter.response.send_message = AsyncMock()
    view = StartOverView(ctx)
    await view.start_over.callback(inter)
    assert lock.locked() is False
    assert isinstance(inter.response.send_message.call_args.kwargs["view"], FallbackMenuView)


def test_resubmitting_the_profile_updates_instead_of_failing() -> None:
    from bot.db import djs

    assert "ON CONFLICT (discord_user_id) DO UPDATE" in inspect.getsource(djs.insert_dj)


@pytest.mark.asyncio
async def test_dm_buttons_survive_a_restart() -> None:
    from bot.ui.views import FallbackMenuView, OnboardingIntroView, StartOverView

    ctx = MagicMock()
    for view in (FallbackMenuView(ctx), OnboardingIntroView(ctx, 0, 0), StartOverView(ctx)):
        assert view.is_persistent(), type(view).__name__
    from bot import main

    source = inspect.getsource(main.main)
    for name in ("FallbackMenuView(ctx)", "OnboardingIntroView(ctx", "StartOverView(ctx)"):
        assert f"bot.add_view({name}" in source, name
