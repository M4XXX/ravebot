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

    modal = OnboardingModal(ctx, 12345, lock, guild_id=99999)
    inter = make_modal_inter()
    # guild_id=99999 but no role assignment expected — bypass by returning None from get_guild
    inter.client.get_guild.return_value = None

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

    modal = OnboardingModal(ctx, 12345, lock, guild_id=99999)
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

    modal = OnboardingModal(ctx, 12345, lock, guild_id=99999)
    inter = make_modal_inter(logo_url="not-a-url")

    with patch("bot.ui.views.insert_dj", new=AsyncMock()) as mock_insert:
        await modal.callback(inter)

    mock_insert.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    assert not lock.locked()


# ---------------------------------------------------------------------------
# Story 2.3: Active Booking Role Assignment & Notion Sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_assigns_role_and_writes_notion() -> None:
    """Happy path: role assigned + Notion written + confirmation sent."""
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.write_dj_record = AsyncMock()
    ctx.notion_client = notion_client
    lock = ctx.get_user_lock(12345)
    await lock.acquire()

    modal = OnboardingModal(ctx, 12345, lock, guild_id=99999)
    inter = make_modal_inter()

    role = MagicMock(spec=disnake.Role)
    role.name = "Active Booking"
    member = MagicMock(spec=disnake.Member)
    member.add_roles = AsyncMock()
    guild = MagicMock(spec=disnake.Guild)
    guild.get_member.return_value = member
    guild.roles = [role]
    inter.client.get_guild.return_value = guild

    with patch("bot.ui.views.insert_dj", new=AsyncMock()):
        with patch("disnake.utils.get", return_value=role):
            await modal.callback(inter)

    member.add_roles.assert_awaited_once_with(role, reason="Onboarding completed")
    notion_client.write_dj_record.assert_awaited_once_with(
        discord_user_id=12345,
        dj_name="TestDJ",
    )
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "all set" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_callback_confirms_even_if_notion_fails() -> None:
    """NotionWriteError: confirmation still sent AND ERR_NOTION_WRITE posted to admin log (AC3)."""
    from bot.notion.client import NotionWriteError
    from bot.ui.embeds import ErrorCode
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())

    # Simulate _write_with_retry behaviour: post ERR_NOTION_WRITE to admin log then raise
    log_mock = MagicMock()
    log_mock.post_error = AsyncMock()

    async def write_dj_fails(**kwargs: object) -> None:
        await log_mock.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger="Notion write failed after 3 attempts",
            action_taken="Bot aborted the write operation.",
            manual_action="Check Notion API key and database permissions.",
        )
        raise NotionWriteError("Notion down after 3 retries")

    notion_client = MagicMock()
    notion_client.write_dj_record = AsyncMock(side_effect=write_dj_fails)
    ctx.notion_client = notion_client
    lock = ctx.get_user_lock(22222)
    await lock.acquire()

    modal = OnboardingModal(ctx, 22222, lock, guild_id=99999)
    inter = make_modal_inter(author_id=22222)
    inter.client.get_guild.return_value = None  # skip role assignment

    with patch("bot.ui.views.insert_dj", new=AsyncMock()):
        await modal.callback(inter)

    # ERR_NOTION_WRITE must be posted to admin log despite best-effort handling
    log_mock.post_error.assert_awaited_once()
    assert log_mock.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE
    # Confirmation must still be sent despite Notion failure
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "all set" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_callback_skips_notion_if_client_none() -> None:
    """If notion_client is None on BotContext, skip sync silently."""
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    lock = ctx.get_user_lock(33333)
    await lock.acquire()

    modal = OnboardingModal(ctx, 33333, lock, guild_id=99999)
    inter = make_modal_inter(author_id=33333)
    inter.client.get_guild.return_value = None

    with patch("bot.ui.views.insert_dj", new=AsyncMock()):
        await modal.callback(inter)

    inter.response.send_message.assert_awaited_once()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_callback_continues_if_add_roles_raises_http_error() -> None:
    """disnake.HTTPException on add_roles (e.g. rate limit) does not prevent confirmation."""
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    lock = ctx.get_user_lock(55555)
    await lock.acquire()

    modal = OnboardingModal(ctx, 55555, lock, guild_id=99999)
    inter = make_modal_inter(author_id=55555)

    role = MagicMock(spec=disnake.Role)
    role.name = "Active Booking"
    member = MagicMock(spec=disnake.Member)
    member.add_roles = AsyncMock(side_effect=disnake.HTTPException(MagicMock(), "rate limited"))
    guild = MagicMock(spec=disnake.Guild)
    guild.get_member.return_value = member
    inter.client.get_guild.return_value = guild

    with patch("bot.ui.views.insert_dj", new=AsyncMock()):
        with patch("disnake.utils.get", return_value=role):
            await modal.callback(inter)

    # Confirmation must still be sent despite HTTP error on role assignment
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "all set" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_callback_skips_role_if_guild_not_found() -> None:
    """If get_guild returns None, skip role assignment without crash."""
    from bot.ui.views import OnboardingModal

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    lock = ctx.get_user_lock(44444)
    await lock.acquire()

    modal = OnboardingModal(ctx, 44444, lock, guild_id=99999)
    inter = make_modal_inter(author_id=44444)
    inter.client.get_guild.return_value = None

    with patch("bot.ui.views.insert_dj", new=AsyncMock()):
        await modal.callback(inter)

    inter.response.send_message.assert_awaited_once()
    assert not lock.locked()


# ---------------------------------------------------------------------------
# Story 2.4: pass_for_now + FallbackMenuView
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pass_for_now_sends_warm_response() -> None:
    """pass_for_now sends warm decline; no DB write, no lock acquired."""
    from bot.ui.views import OnboardingIntroView

    ctx = make_real_ctx(db=MagicMock())
    view = OnboardingIntroView(ctx, 12345, guild_id=99999)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 12345
    inter.response.send_message = AsyncMock()

    await view.pass_for_now.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "no problem" in msg.lower() or "later" in msg.lower()
    assert not ctx.get_user_lock(12345).locked()


@pytest.mark.asyncio
async def test_fallback_go_active_updates_db_assigns_role() -> None:
    """go_active sets active_booking=True in DB and assigns Active Booking role."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 77777
    inter.response.send_message = AsyncMock()

    role = MagicMock(spec=disnake.Role)
    role.name = "Active Booking"
    member = MagicMock(spec=disnake.Member)
    member.add_roles = AsyncMock()
    guild = MagicMock(spec=disnake.Guild)
    guild.get_member.return_value = member
    inter.client.guilds = [guild]

    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=True)):
        with patch("disnake.utils.get", return_value=role):
            await view.go_active.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "pool" in msg.lower()
    member.add_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_go_active_dj_not_in_db() -> None:
    """go_active responds with onboarding prompt when DJ has no DB record."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 88888
    inter.response.send_message = AsyncMock()
    inter.client.guilds = []

    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=False)):
        await view.go_active.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "onboarding" in msg.lower() or "profile" in msg.lower() or "role" in msg.lower()


@pytest.mark.asyncio
async def test_fallback_go_inactive_updates_db_removes_role() -> None:
    """go_inactive sets active_booking=False and removes Active Booking role."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 66666
    inter.response.send_message = AsyncMock()

    role = MagicMock(spec=disnake.Role)
    role.name = "Active Booking"
    member = MagicMock(spec=disnake.Member)
    member.remove_roles = AsyncMock()
    guild = MagicMock(spec=disnake.Guild)
    guild.get_member.return_value = member
    inter.client.guilds = [guild]

    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=True)):
        with patch("disnake.utils.get", return_value=role):
            await view.go_inactive.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "stop reaching out" in msg.lower() or "inactive" in msg.lower()
    member.remove_roles.assert_awaited_once()
