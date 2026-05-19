"""Tests for bot/ui/views.py — OnboardingModal and RetryOnboardingModalView."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from tests.conftest import make_real_ctx


def make_modal_inter(
    dj_name: str = "TestDJ",
    vrchat_name: str = "",
    logo_url: str = "",
    stream_url_primary: str = "https://twitch.tv/testdj",
    stream_url_backup: str = "",
    author_id: int = 12345,
) -> MagicMock:
    """Helper: mock ModalInteraction with text_values."""
    inter = MagicMock(spec=disnake.ModalInteraction)
    inter.author.id = author_id
    inter.text_values = {
        "dj_name": dj_name,
        "vrchat_name": vrchat_name,
        "logo_url": logo_url,
        "stream_url_primary": stream_url_primary,
        "stream_url_backup": stream_url_backup,
    }
    inter.response.send_message = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_modal_callback_inserts_dj_and_confirms() -> None:
    """Valid submission inserts DJ, sends confirmation, and releases the lock."""
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(12345)
    await lock.acquire()

    modal = OnboardingModal(ctx, 12345, lock)
    inter = make_modal_inter()

    with patch("bot.ui.views.insert_dj", new=AsyncMock()) as mock_insert:
        await modal.callback(inter)

    mock_insert.assert_awaited_once()
    insert_kwargs = mock_insert.call_args.kwargs
    assert insert_kwargs["discord_user_id"] == 12345
    assert insert_kwargs["dj_name"] == "TestDJ"
    assert insert_kwargs["stream_url_primary"] == "https://twitch.tv/testdj"
    assert insert_kwargs["vrchat_name"] is None
    assert insert_kwargs["logo_url"] is None
    assert insert_kwargs["stream_url_backup"] is None

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "all set" in msg.lower()

    assert not lock.locked()  # Lock must be released


@pytest.mark.asyncio
async def test_modal_callback_rejects_invalid_stream_url() -> None:
    """Invalid Primary Stream URL releases lock and sends retry view."""
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(12345)
    await lock.acquire()

    modal = OnboardingModal(ctx, 12345, lock)
    inter = make_modal_inter(stream_url_primary="not-a-url")

    with patch("bot.ui.views.insert_dj", new=AsyncMock()) as mock_insert:
        await modal.callback(inter)

    mock_insert.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "view" in call_kwargs  # RetryOnboardingModalView passed
    assert not lock.locked()  # Lock released on validation failure


@pytest.mark.asyncio
async def test_modal_callback_rejects_invalid_logo_url() -> None:
    """Invalid Logo URL (non-empty but bad format) releases lock and sends retry."""
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(12345)
    await lock.acquire()

    modal = OnboardingModal(ctx, 12345, lock)
    inter = make_modal_inter(logo_url="not-a-url")

    with patch("bot.ui.views.insert_dj", new=AsyncMock()) as mock_insert:
        await modal.callback(inter)

    mock_insert.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    assert not lock.locked()
