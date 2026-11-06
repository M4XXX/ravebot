"""Exact catalog strings in the onboarding flow (Task 9)."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake

from bot.ui.views import OnboardingIntroView, OnboardingModal, RetryOnboardingModalView
from tests.conftest import make_real_ctx


def _inter() -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 12345
    inter.response.send_message = AsyncMock()
    return inter


def _modal_inter(**over) -> MagicMock:
    values = {
        "dj_name": "DJ",
        "vrchat_name": "",
        "logo_url": "",
        "stream_url_primary": "https://twitch.tv/dj",
        "stream_url_backup": "",
    }
    values.update(over)
    inter = MagicMock(spec=disnake.ModalInteraction)
    inter.author.id = 12345
    inter.text_values = values
    inter.response.send_message = AsyncMock()
    inter.client.get_guild.return_value = None
    return inter


async def test_intro_buttons_exact_labels() -> None:
    view = OnboardingIntroView(make_real_ctx(db=MagicMock()), 1, 2)
    assert [c.label for c in view.children] == ["Join the DJ pool", "Not now"]


async def test_retry_button_label() -> None:
    view = RetryOnboardingModalView(make_real_ctx(db=MagicMock()), 1, 2)
    assert [c.label for c in view.children] == ["Try again"]


async def test_declined_reply_exact() -> None:
    view = OnboardingIntroView(make_real_ctx(db=MagicMock()), 12345, 2)
    inter = _inter()
    await view.pass_for_now.callback(inter)
    assert inter.response.send_message.call_args[0][0] == (
        "No problem. Write me any message to open the menu and join later."
    )


async def test_done_reply_exact() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(12345)
    await lock.acquire()
    modal = OnboardingModal(ctx, 12345, lock, guild_id=9)
    inter = _modal_inter()
    with patch("bot.ui.views.insert_dj", new=AsyncMock()):
        await modal.callback(inter)
    assert inter.response.send_message.call_args[0][0] == (
        "You're in the DJ pool. I'll message you when a slot opens. "
        "You can book any time from the menu."
    )


async def test_invalid_link_text_names_fields() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(12345)
    await lock.acquire()
    modal = OnboardingModal(ctx, 12345, lock, guild_id=9)
    inter = _modal_inter(stream_url_primary="nope", logo_url="bad")
    await modal.callback(inter)
    assert inter.response.send_message.call_args[0][0] == (
        "That link doesn't look right: Logo URL, Primary Stream URL. "
        'Links must start with https://. Tap "Try again" to fix it.'
    )
    assert not lock.locked()


async def test_flow_open_reply_exact() -> None:
    ctx = make_real_ctx(db=MagicMock())
    await ctx.get_user_lock(12345).acquire()
    for view, button in (
        (OnboardingIntroView(ctx, 12345, 2), "go_active"),
        (RetryOnboardingModalView(ctx, 12345, 2), "retry"),
    ):
        inter = _inter()
        await getattr(view, button).callback(inter)
        assert inter.response.send_message.call_args[0][0] == (
            "You already have a booking open. Finish or dismiss it first."
        )
