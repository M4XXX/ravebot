"""Tests for bot/ui/views.py — OnboardingModal and RetryOnboardingModalView."""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from tests.conftest import make_real_ctx


@pytest.fixture
def registered_dj():
    """The user finished onboarding and holds the DJ role — booking is allowed."""
    with patch("bot.ui.views.is_registered_dj", new=AsyncMock(return_value=True)):
        yield


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
    dj_role = MagicMock(spec=disnake.Role)
    dj_role.name = "DJ"
    member.roles = [dj_role]  # Go Active needs the DJ role
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


# ---------------------------------------------------------------------------
# Story 3.3: FallbackMenuView.show_lineup()
# ---------------------------------------------------------------------------

def _make_notion_event(
    name: str = "Friday Night Rave",
    date_str: str = "2026-06-13T20:00:00+00:00",
    slots: int = 3,
    djs: int = 1,
) -> dict:
    return {
        "id": "page-uuid",
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Date": {"date": {"start": date_str}},
            "Slots": {"number": slots},
            "DJs": {"relation": [{"id": f"dj-{i}"} for i in range(djs)]},
        },
    }


@pytest.mark.asyncio
async def test_show_lineup_sends_embed_for_first_event() -> None:
    """Events returned → MissionOverviewEmbed sent for first event."""
    from bot.ui.embeds import MissionOverviewEmbed
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(return_value=[_make_notion_event()])
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in call_kwargs
    assert isinstance(call_kwargs["embed"], MissionOverviewEmbed)


@pytest.mark.asyncio
async def test_show_lineup_sends_no_events_message_when_empty() -> None:
    """Empty events list → 'No upcoming events' message sent."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "no upcoming events" in msg.lower()


@pytest.mark.asyncio
async def test_show_lineup_handles_notion_client_none() -> None:
    """notion_client is None → friendly fallback message sent, no crash."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "available" in msg.lower() or "check back" in msg.lower()


@pytest.mark.asyncio
async def test_show_lineup_handles_poll_exception() -> None:
    """poll_events raises → 'Something went wrong' message sent, no crash."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(side_effect=Exception("Notion down"))
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "couldn't load" in msg.lower()


# ---------------------------------------------------------------------------
# Story 4.1: FallbackMenuView.book_slot + EventSelectionView + SlotSelectionView
# ---------------------------------------------------------------------------

def _make_event(
    event_id: str = "evt-001",
    name: str = "Test Rave",
    slots: int = 6,
    djs_booked: int = 2,
    date_str: str = "2026-07-01T20:00:00+00:00",
) -> dict:
    """Helper: minimal Notion event page object."""
    return {
        "id": event_id,
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Date": {"date": {"start": date_str}},
            "Slots": {"number": slots},
            "DJs": {"relation": [{"id": f"dj-{i}"} for i in range(djs_booked)]},
        },
    }


@pytest.mark.asyncio
async def test_book_slot_lock_already_held_returns_message(registered_dj) -> None:
    """Lock already acquired → 'active booking session' message, no re-acquire. (AC4)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 11111
    inter.response.send_message = AsyncMock()

    # Pre-acquire lock to simulate active flow
    lock = ctx.get_user_lock(11111)
    await lock.acquire()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "active booking session" in msg.lower() or "already" in msg.lower()
    assert lock.locked()  # Lock was NOT acquired again


@pytest.mark.asyncio
async def test_book_slot_notion_client_none_releases_lock(registered_dj) -> None:
    """notion_client=None → error message sent, lock acquired then released. (AC6)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 22222
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    assert not ctx.get_user_lock(22222).locked()  # Lock released


@pytest.mark.asyncio
async def test_book_slot_poll_events_raises_releases_lock(registered_dj) -> None:
    """poll_events raises → error message sent, lock released. (AC6)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(side_effect=Exception("Notion down"))
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 33333
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    assert not ctx.get_user_lock(33333).locked()  # Lock released


@pytest.mark.asyncio
async def test_book_slot_all_events_full_releases_lock(registered_dj) -> None:
    """All events fully booked → 'All slots filled' message, lock released. (AC3)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    # Event with all 6 slots booked
    notion_client.poll_events = AsyncMock(return_value=[_make_event(slots=6, djs_booked=6)])
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 44444
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "all slots" in msg.lower() or "filled" in msg.lower()
    assert not ctx.get_user_lock(44444).locked()  # Lock released


@pytest.mark.asyncio
async def test_book_slot_no_events_at_all_releases_lock(registered_dj) -> None:
    """poll_events returns [] → 'All slots filled' message, lock released. (AC3)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 55555
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    assert not ctx.get_user_lock(55555).locked()


@pytest.mark.asyncio
async def test_book_slot_open_slots_shows_event_selection_view(registered_dj) -> None:
    """Events with open slots → message with EventSelectionView sent. (AC1)"""
    from bot.ui.views import EventSelectionView, FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.poll_events = AsyncMock(
        return_value=[_make_event(slots=6, djs_booked=2)]
    )
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 66666
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    call_kwargs = call_args.kwargs
    assert "view" in call_kwargs
    assert isinstance(call_kwargs["view"], EventSelectionView)
    # Message content must include a Discord timestamp
    content = call_args[0][0] if call_args[0] else call_kwargs.get("content", "")
    assert "<t:" in content
    # Lock is held (passed to EventSelectionView)
    assert ctx.get_user_lock(66666).locked()


@pytest.mark.asyncio
async def test_event_selection_never_mind_releases_lock() -> None:
    """[Never mind] in EventSelectionView → 'No problem' message, lock released. (AC5)"""
    from bot.ui.views import EventSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(77777)
    await lock.acquire()
    view = EventSelectionView(ctx, [_make_event()], lock, 77777)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_cancel(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "no problem" in msg.lower() or "nothing was booked" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_event_selection_handle_event_sends_embed_and_slot_view() -> None:
    """Event button tapped → MissionOverviewEmbed + SlotSelectionView sent. (AC2)"""
    from bot.ui.embeds import MissionOverviewEmbed
    from bot.ui.views import EventSelectionView, SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(88888)
    await lock.acquire()
    event = _make_event(slots=6, djs_booked=2)
    view = EventSelectionView(ctx, [event], lock, 88888)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_event(inter, event)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in call_kwargs
    assert isinstance(call_kwargs["embed"], MissionOverviewEmbed)
    assert "view" in call_kwargs
    assert isinstance(call_kwargs["view"], SlotSelectionView)
    assert lock.locked()  # Lock still held — passed through to SlotSelectionView


@pytest.mark.asyncio
async def test_slot_selection_never_mind_releases_lock() -> None:
    """[Never mind] in SlotSelectionView → 'No problem' message, lock released. (AC5)"""
    from bot.ui.views import SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(99999)
    await lock.acquire()
    event = _make_event(slots=6, djs_booked=2)
    open_slots = list(range(2, 6))  # slots 2–5 are open
    view = SlotSelectionView(ctx, event, open_slots, lock, 99999)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_cancel(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "no problem" in msg.lower() or "nothing was booked" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_slot_selection_handle_slot_presents_booking_confirm_view() -> None:
    """Slot button tapped → BookingConfirmEmbed + BookingConfirmView presented, lock held."""
    from bot.ui.embeds import BookingConfirmEmbed
    from bot.ui.views import BookingConfirmView, SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(11111)
    await lock.acquire()
    event = _make_event(slots=6, djs_booked=2)
    open_slots = list(range(2, 6))
    view = SlotSelectionView(ctx, event, open_slots, lock, 11111)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_slot(inter, 2)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in call_kwargs
    assert isinstance(call_kwargs["embed"], BookingConfirmEmbed)
    assert "view" in call_kwargs
    assert isinstance(call_kwargs["view"], BookingConfirmView)
    # Lock is still held — passed into BookingConfirmView
    assert lock.locked()


# ---------------------------------------------------------------------------
# Story 4.2: BookingConfirmView
# ---------------------------------------------------------------------------

def _make_booking_confirm_view(
    ctx: object,
    lock: object,
    user_id: int = 12345,
    slots: int = 6,
    djs_booked: int = 2,
    slot_index: int = 2,
) -> "object":
    """Helper: build a BookingConfirmView with a pre-made event."""
    from bot.ui.views import BookingConfirmView

    event = _make_event(slots=slots, djs_booked=djs_booked)
    return BookingConfirmView(
        ctx=ctx,
        event=event,
        slot_index=slot_index,
        slot_unix=1751400000,
        lock=lock,
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_booking_confirm_cancel_releases_lock() -> None:
    """[✖ Cancel] → 'No problem' message, per-user lock released. (AC4)"""

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20001)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20001)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_cancel(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "no problem" in msg.lower() or "nothing was booked" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_db_none_releases_lock() -> None:
    """ctx.db is None → error message sent, lock released. (AC6)"""
    ctx = make_real_ctx(db=None)
    lock = ctx.get_user_lock(20002)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20002)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_confirm(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "something went wrong" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_slot_taken_presents_slot_selection_view() -> None:
    """acquire_slot_lock returns False → slot-taken msg + refreshed SlotSelectionView. (AC5)"""
    from bot.ui.views import SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()  # must pass notion_client guard to reach slot-lock check
    ctx.notion_client.poll_events = AsyncMock(return_value=[])  # Story 4.3: poll called in slot-taken path
    lock = ctx.get_user_lock(20003)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20003)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=False)), \
         patch("bot.db.bookings.get_slot_lock_holder", new=AsyncMock(return_value=None)):
        await view._handle_confirm(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "taken" in msg.lower() or "sorry" in msg.lower()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert isinstance(call_kwargs.get("view"), SlotSelectionView)
    assert lock.locked()  # Lock stays held — passed into the refreshed SlotSelectionView


@pytest.mark.asyncio
async def test_booking_confirm_notion_failure_releases_lock() -> None:
    """NotionWriteError on write_booking → error msg, slot lock and per-user lock released."""
    from bot.notion.client import NotionWriteError

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20004)
    await lock.acquire()

    notion_client = MagicMock()
    notion_client.write_booking = AsyncMock(side_effect=NotionWriteError("fail"))
    ctx.notion_client = notion_client

    view = _make_booking_confirm_view(ctx, lock, user_id=20004)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()) as mock_release, \
         patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value=None)):
        await view._handle_confirm(inter)

    mock_release.assert_awaited_once()
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "something went wrong" in msg.lower()
    assert not lock.locked()


@pytest.mark.parametrize("failure", ["page_lookup", "unexpected_write_error"])
@pytest.mark.asyncio
async def test_booking_confirm_gives_the_slot_back_on_any_failure_after_the_claim(failure) -> None:
    """Once slot_locks says 'ours', no failure may leave the slot booked behind the DJ's back."""
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20005)
    await lock.acquire()
    ctx.notion_client = MagicMock()
    ctx.notion_client.write_booking = AsyncMock(
        side_effect=RuntimeError("unexpected") if failure == "unexpected_write_error" else None
    )
    page_lookup = AsyncMock(
        side_effect=ConnectionError("db hiccup") if failure == "page_lookup" else None,
        return_value="page",
    )
    view = _make_booking_confirm_view(ctx, lock, user_id=20005)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()) as mock_release, \
         patch("bot.db.djs.get_notion_dj_page_id", new=page_lookup):
        await view._handle_confirm(inter)

    mock_release.assert_awaited_once()
    assert mock_release.call_args.args[3] == 20005  # only the DJ's own claim is undone
    assert "something went wrong" in inter.response.send_message.call_args[0][0].lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_success_sends_confirmed_embed() -> None:
    """Full success path → BookingConfirmedEmbed sent, per-user lock released. (AC3)"""
    from bot.ui.embeds import BookingConfirmedEmbed

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20005)
    await lock.acquire()

    notion_client = MagicMock()
    notion_client.write_booking = AsyncMock()
    notion_client.poll_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client

    view = _make_booking_confirm_view(ctx, lock, user_id=20005)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value="notion-dj-001")):
        await view._handle_confirm(inter)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in call_kwargs
    assert isinstance(call_kwargs["embed"], BookingConfirmedEmbed)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_success_triggers_lineup_post() -> None:
    """Success path with lineup_service set → post_update awaited with fresh events. (AC3)"""
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20006)
    await lock.acquire()

    fresh_events = [_make_event()]
    notion_client = MagicMock()
    notion_client.write_booking = AsyncMock()
    notion_client.poll_events = AsyncMock(return_value=fresh_events)
    ctx.notion_client = notion_client

    lineup_service = MagicMock()
    lineup_service.post_update = AsyncMock()
    ctx.lineup_service = lineup_service

    view = _make_booking_confirm_view(ctx, lock, user_id=20006)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value="dj-page")):
        await view._handle_confirm(inter)

    lineup_service.post_update.assert_awaited_once_with(fresh_events)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_success_posts_admin_log() -> None:
    """Success path → AdminLogService.post_interaction called with booking details. (AC3)"""
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20007)
    await lock.acquire()

    notion_client = MagicMock()
    notion_client.write_booking = AsyncMock()
    notion_client.poll_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client

    log_service = MagicMock()
    log_service.post_interaction = AsyncMock()
    ctx.log_service = log_service

    view = _make_booking_confirm_view(ctx, lock, user_id=20007, slot_index=2)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.__str__ = lambda self: "DJ_Test#1234"
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value="dj-page")):
        await view._handle_confirm(inter)

    log_service.post_interaction.assert_awaited_once()
    call_kwargs = log_service.post_interaction.call_args.kwargs
    assert call_kwargs["action_type"] == "Booking"
    assert "3" in call_kwargs["outcome"]  # slot_index=2 → "Slot 3 confirmed"
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_notion_client_none_releases_lock() -> None:
    """ctx.notion_client is None → error message sent, lock released, no DB slot claimed."""
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = None
    lock = ctx.get_user_lock(20008)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20008)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_confirm(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "something went wrong" in msg.lower()
    assert not lock.locked()


# ---------------------------------------------------------------------------
# Story 4.3: Race condition handling tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_booking_confirm_slot_conflict_logs_err_slot_conflict() -> None:
    """acquire_slot_lock returns False → ERR_SLOT_CONFLICT posted with both user IDs. (AC2)"""
    from bot.ui.embeds import ErrorCode

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(return_value=[])
    log_service = MagicMock()
    log_service.post_error = AsyncMock()
    ctx.log_service = log_service
    lock = ctx.get_user_lock(20009)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20009)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=False)), \
         patch("bot.db.bookings.get_slot_lock_holder", new=AsyncMock(return_value=99999)):
        await view._handle_confirm(inter)

    log_service.post_error.assert_awaited_once()
    call_kwargs = log_service.post_error.call_args.kwargs
    assert call_kwargs["error_code"] == ErrorCode.ERR_SLOT_CONFLICT
    assert str(20009) in call_kwargs["trigger"]   # losing DJ's user_id
    assert str(99999) in call_kwargs["trigger"]   # winning DJ's user_id
    assert lock.locked()  # Lock still held — passed to refreshed SlotSelectionView


@pytest.mark.asyncio
async def test_booking_confirm_slot_conflict_uses_fresh_notion_data() -> None:
    """acquire_slot_lock returns False → poll_events called, SlotSelectionView uses fresh event. (AC1)"""
    from bot.ui.views import SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    # Return a fresh event with the same ID but updated data (8 slots instead of 6)
    fresh_event = _make_event(event_id="evt-001", name="Fresh Rave", slots=8, djs_booked=2)
    ctx.notion_client.poll_events = AsyncMock(return_value=[fresh_event])
    lock = ctx.get_user_lock(20010)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20010)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=False)):
        await view._handle_confirm(inter)

    ctx.notion_client.poll_events.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    presented_view = call_kwargs.get("view")
    assert isinstance(presented_view, SlotSelectionView)
    assert presented_view._event is fresh_event  # fresh data used, not stale self._event
    msg = inter.response.send_message.call_args[0][0]
    assert "remaining open slots" in msg.lower()


@pytest.mark.asyncio
async def test_booking_confirm_slot_conflict_all_slots_taken() -> None:
    """acquire_slot_lock returns False and no slots remain → lock released, 'fully booked' message."""
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    # All 6 slots booked — remaining will be [] after excluding the conflicting slot_index=2
    ctx.notion_client.poll_events = AsyncMock(
        return_value=[_make_event(event_id="evt-001", slots=6, djs_booked=6)]
    )
    lock = ctx.get_user_lock(20012)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20012, slots=6, djs_booked=6)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=False)):
        await view._handle_confirm(inter)

    assert not lock.locked()  # lock released — no slots to offer
    msg = inter.response.send_message.call_args[0][0]
    assert "fully booked" in msg.lower()


@pytest.mark.asyncio
async def test_booking_confirm_pg_failure_releases_lock() -> None:
    """acquire_slot_lock raises Exception → lock released, error message, Notion not written. (AC3)"""
    ctx = make_real_ctx(db=MagicMock())
    # notion_client must NOT be None — the Notion guard runs before acquire_slot_lock
    ctx.notion_client = MagicMock()
    ctx.notion_client.write_booking = AsyncMock()
    lock = ctx.get_user_lock(20011)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20011)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch(
        "bot.db.bookings.acquire_slot_lock",
        new=AsyncMock(side_effect=Exception("pg connection lost")),
    ):
        await view._handle_confirm(inter)

    assert not lock.locked()  # per-user lock released
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "something went wrong" in msg.lower() or "try again" in msg.lower()
    ctx.notion_client.write_booking.assert_not_called()  # Notion NOT written


# ---------------------------------------------------------------------------
# Story 4.4: FallbackMenuView.cancel_slot + CancellationView + CancelConfirmView
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_slot_lock_already_held() -> None:
    """Lock pre-acquired → 'active booking session' message, lock NOT re-acquired. (AC6)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    view = FallbackMenuView(ctx)
    lock = ctx.get_user_lock(30001)
    await lock.acquire()

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 30001
    inter.response.send_message = AsyncMock()

    await view.cancel_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "active booking session" in msg.lower() or "already" in msg.lower()
    assert lock.locked()  # lock was NOT released


@pytest.mark.asyncio
async def test_cancel_slot_no_bookings_releases_lock() -> None:
    """get_dj_slot_locks returns [] → 'no upcoming bookings', lock released. (AC2)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(return_value=[])
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 30002
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=[])):
        await view.cancel_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "no upcoming bookings" in msg.lower() or "don't have any" in msg.lower()
    assert not ctx.get_user_lock(30002).locked()  # lock released


@pytest.mark.asyncio
async def test_cancel_slot_presents_cancellation_view() -> None:
    """1 booking found in slot_locks + matching event → CancellationView presented, lock held. (AC1)"""
    from bot.ui.views import CancellationView, FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    event = _make_event(event_id="evt-cancel-01", djs_booked=1)
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(return_value=[event])
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 30003
    inter.response.send_message = AsyncMock()

    slot_rows = [{"event_id": "evt-cancel-01", "slot_index": 0}]
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=slot_rows)):
        await view.cancel_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "view" in call_kwargs
    assert isinstance(call_kwargs["view"], CancellationView)
    assert ctx.get_user_lock(30003).locked()  # lock still held


@pytest.mark.asyncio
async def test_cancellation_view_never_mind_releases_lock() -> None:
    """[Never mind] button → lock released, warm message sent."""
    from bot.ui.views import CancellationView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(30004)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-02", djs_booked=1)
    bookings = [{"event": event, "slot_index": 0, "slot_unix": 1751414400}]
    view = CancellationView(ctx, bookings, lock, user_id=30004)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_never_mind(inter)

    assert not lock.locked()  # lock released
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "no problem" in msg.lower() or "nothing" in msg.lower()


@pytest.mark.asyncio
async def test_cancel_confirm_keep_releases_lock() -> None:
    """[Keep It] → 'No changes made', lock released. (AC5)"""
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(30005)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-03", djs_booked=1)
    view = CancelConfirmView(ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=30005)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_keep(inter)

    assert not lock.locked()  # lock released
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "no changes" in msg.lower() or "still booked" in msg.lower()


@pytest.mark.asyncio
async def test_cancel_confirm_confirm_calls_delete_booking() -> None:
    """Confirm tapped → delete_booking called, release_slot_lock called, confirmed message. (AC4)"""
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(
        return_value=[_make_event(event_id="evt-cancel-04", djs_booked=1)]
    )
    ctx.notion_client.delete_booking = AsyncMock()
    ctx.log_service = None
    ctx.lineup_service = None
    lock = ctx.get_user_lock(30006)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-04", djs_booked=1)
    view = CancelConfirmView(ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=30006)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()

    with patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value="notion-page-dj")), \
         patch("bot.db.bookings.holds_slot_in_event", new=AsyncMock(return_value=False)), \
         patch("bot.db.bookings.release_slot_lock",
               new=AsyncMock(return_value=True)) as mock_release:
        await view._handle_confirm(inter)

    ctx.notion_client.delete_booking.assert_awaited_once()
    mock_release.assert_awaited_once_with(ctx.db, "evt-cancel-04", 0, 30006)
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "canceled" in msg.lower()
    assert not lock.locked()  # lock released


@pytest.mark.asyncio
async def test_cancel_confirm_notion_write_error_releases_lock() -> None:
    """delete_booking raises NotionWriteError: slot stays freed, DJ told it is cancelled."""
    from bot.notion.client import NotionWriteError
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(
        return_value=[_make_event(event_id="evt-cancel-05", djs_booked=1)]
    )
    ctx.notion_client.delete_booking = AsyncMock(side_effect=NotionWriteError("write failed"))
    ctx.log_service = None
    ctx.lineup_service = None
    lock = ctx.get_user_lock(30007)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-05", djs_booked=1)
    view = CancelConfirmView(ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=30007)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()

    with patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value="notion-page-dj")), \
         patch("bot.db.bookings.holds_slot_in_event", new=AsyncMock(return_value=False)), \
         patch("bot.db.bookings.release_slot_lock",
               new=AsyncMock(return_value=True)) as mock_release:
        await view._handle_confirm(inter)

    # The slot is freed before Notion is touched; a Notion failure only leaves drift that
    # the client logs and reconcile reports
    mock_release.assert_awaited_once_with(ctx.db, "evt-cancel-05", 0, 30007)
    ctx.notion_client.delete_booking.assert_awaited_once()
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "canceled" in msg.lower()
    assert not lock.locked()  # lock released


async def _cancel_view(user_id: int, event_id: str):
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.delete_booking = AsyncMock()
    ctx.log_service = None
    ctx.lineup_service = None
    lock = ctx.get_user_lock(user_id)
    await lock.acquire()
    event = _make_event(event_id=event_id, djs_booked=1)
    view = CancelConfirmView(
        ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=user_id
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()
    return ctx, lock, view, inter


@pytest.mark.asyncio
async def test_cancel_confirm_for_a_slot_no_longer_mine_changes_nothing() -> None:
    """An old cancel button for a slot that changed hands frees nothing and unlinks nothing."""
    ctx, lock, view, inter = await _cancel_view(30008, "evt-cancel-06")
    with patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value="notion-page-dj")), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock(return_value=False)):
        await view._handle_confirm(inter)
    ctx.notion_client.delete_booking.assert_not_awaited()
    msg = inter.response.send_message.call_args[0][0]
    assert "isn't booked by you anymore" in msg
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancel_confirm_keeps_notion_link_while_another_slot_is_held() -> None:
    """Cancelling one of two slots in an event keeps the DJ in the Notion lineup."""
    ctx, lock, view, inter = await _cancel_view(30009, "evt-cancel-07")
    with patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value="notion-page-dj")), \
         patch("bot.db.bookings.holds_slot_in_event", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock(return_value=True)):
        await view._handle_confirm(inter)
    ctx.notion_client.delete_booking.assert_not_awaited()
    assert "canceled" in inter.response.send_message.call_args[0][0].lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancel_slot_poll_events_failure_releases_lock() -> None:
    """poll_events raises after slot_lock_rows found → error message, lock released."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.poll_events = AsyncMock(side_effect=RuntimeError("network error"))
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 30008
    inter.response.send_message = AsyncMock()

    slot_rows = [{"event_id": "evt-stale-01", "slot_index": 0}]
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=slot_rows)):
        await view.cancel_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "something went wrong" in msg.lower() or "try again" in msg.lower()
    assert not ctx.get_user_lock(30008).locked()  # lock released


@pytest.mark.asyncio
async def test_cancel_slot_all_events_expired_releases_lock() -> None:
    """slot_lock_rows found but no matching Notion event → 'no upcoming bookings', lock released."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    # poll_events returns an event with a DIFFERENT id — no match for the slot_lock row
    ctx.notion_client.poll_events = AsyncMock(
        return_value=[_make_event(event_id="evt-other-99", djs_booked=0)]
    )
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 30009
    inter.response.send_message = AsyncMock()

    slot_rows = [{"event_id": "evt-gone-01", "slot_index": 0}]
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=slot_rows)):
        await view.cancel_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "no upcoming bookings" in msg.lower() or "don't have any" in msg.lower()
    assert not ctx.get_user_lock(30009).locked()  # lock released


@pytest.mark.asyncio
async def test_cancellation_view_handle_booking_send_message_failure_releases_lock() -> None:
    """_handle_booking: if send_message raises, lock is released."""
    from bot.ui.views import CancellationView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(30010)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-06", djs_booked=1)
    bookings = [{"event": event, "slot_index": 0, "slot_unix": 1751414400}]
    view = CancellationView(ctx, bookings, lock, user_id=30010)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock(side_effect=RuntimeError("discord error"))

    await view._handle_booking(inter, bookings[0])

    assert not lock.locked()  # lock released despite Discord failure


# ---------------------------------------------------------------------------
# Story 8.3: DJs without a Notion page ID (classic layout)
# ---------------------------------------------------------------------------


def _classic_booking_ctx(user_id: int, ensure_returns: str | None):
    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.per_slot = False
    notion_client.write_booking = AsyncMock()
    notion_client.poll_events = AsyncMock(return_value=[])
    notion_client.ensure_dj_page = AsyncMock(return_value=ensure_returns)
    ctx.notion_client = notion_client
    ctx.log_service = MagicMock()
    ctx.log_service.post_interaction = AsyncMock()
    ctx.log_service.post_error = AsyncMock()
    ctx.lineup_service = None
    return ctx


async def _confirm_classic(ctx, user_id: int, page_id: str | None, dj=None, store=None):
    lock = ctx.get_user_lock(user_id)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=user_id, slot_index=1)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()
    dj_row = {"dj_name": "DJ Nova"} if dj is None else dj
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock(return_value=True)) as rel, \
         patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value=page_id)), \
         patch("bot.ui.views.get_dj_by_discord_id", new=AsyncMock(return_value=dj_row)), \
         patch("bot.notion.dj_pages.set_notion_dj_page_id", new=store or AsyncMock()) as stored:
        await view._handle_confirm(inter)
    return inter, rel, stored, lock


@pytest.mark.asyncio
async def test_classic_booking_without_page_creates_and_links_it() -> None:
    ctx = _classic_booking_ctx(40001, ensure_returns="pg-new")
    inter, rel, stored, lock = await _confirm_classic(ctx, 40001, page_id=None)
    ctx.notion_client.ensure_dj_page.assert_awaited_once_with(40001, "DJ Nova", "Test Rave")
    stored.assert_awaited_once_with(ctx.db, 40001, "pg-new")
    assert ctx.notion_client.write_booking.call_args.kwargs["dj_notion_page_id"] == "pg-new"
    rel.assert_not_awaited()
    outcome = ctx.log_service.post_interaction.call_args.kwargs["outcome"]
    assert "not linked" not in outcome
    assert not lock.locked()


@pytest.mark.asyncio
async def test_classic_booking_keeps_slot_when_no_page_can_be_made() -> None:
    from bot.ui.embeds import BookingConfirmedEmbed

    ctx = _classic_booking_ctx(40002, ensure_returns=None)
    inter, rel, stored, lock = await _confirm_classic(ctx, 40002, page_id=None)
    rel.assert_not_awaited()  # slot stays booked
    stored.assert_not_awaited()
    sent = inter.response.send_message.call_args.kwargs
    assert isinstance(sent["embed"], BookingConfirmedEmbed)
    assert "Notion" not in str(sent["embed"].to_dict())  # never claims a Notion write
    outcome = ctx.log_service.post_interaction.call_args.kwargs["outcome"]
    assert "not linked in Notion" in outcome
    ctx.log_service.post_error.assert_not_awaited()  # ensure_dj_page posts its own error
    assert not lock.locked()


@pytest.mark.asyncio
async def test_classic_booking_with_page_does_not_look_it_up() -> None:
    ctx = _classic_booking_ctx(40003, ensure_returns="unused")
    await _confirm_classic(ctx, 40003, page_id="pg-stored")
    ctx.notion_client.ensure_dj_page.assert_not_awaited()
    assert ctx.notion_client.write_booking.call_args.kwargs["dj_notion_page_id"] == "pg-stored"


@pytest.mark.asyncio
async def test_classic_booking_uses_page_even_if_storing_its_id_fails() -> None:
    ctx = _classic_booking_ctx(40004, ensure_returns="pg-new")
    failing_store = AsyncMock(side_effect=RuntimeError("db down"))
    _, rel, _, _ = await _confirm_classic(ctx, 40004, page_id=None, store=failing_store)
    assert ctx.notion_client.write_booking.call_args.kwargs["dj_notion_page_id"] == "pg-new"
    rel.assert_not_awaited()


@pytest.mark.asyncio
async def test_classic_booking_without_profile_keeps_slot_and_posts_one_error() -> None:
    ctx = _classic_booking_ctx(40005, ensure_returns="unused")
    lock = ctx.get_user_lock(40005)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=40005, slot_index=1)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock(return_value=True)) as rel, \
         patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value=None)), \
         patch("bot.ui.views.get_dj_by_discord_id", new=AsyncMock(return_value=None)):
        await view._handle_confirm(inter)
    ctx.notion_client.ensure_dj_page.assert_not_awaited()
    ctx.log_service.post_error.assert_awaited_once()
    assert "Test Rave" in ctx.log_service.post_error.call_args.kwargs["trigger"]
    rel.assert_not_awaited()


@pytest.mark.asyncio
async def test_classic_cancel_without_page_writes_nothing_and_logs_no_error() -> None:
    from bot.notion.client import NotionClient
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    log = MagicMock()
    log.post_error = AsyncMock()
    log.post_interaction = AsyncMock()
    client = NotionClient(api_key="k", database_id="db", log_service=log)
    client._client.request = AsyncMock()
    client._client.get = AsyncMock()
    client._client.post = AsyncMock()
    client._client.patch = AsyncMock()
    ctx.notion_client = client
    ctx.log_service = log
    ctx.lineup_service = None
    lock = ctx.get_user_lock(40006)
    await lock.acquire()
    event = _make_event(event_id="evt-8-3", djs_booked=1)
    view = CancelConfirmView(
        ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=40006
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()
    with patch("bot.db.djs.get_notion_dj_page_id", new=AsyncMock(return_value=None)), \
         patch("bot.db.bookings.holds_slot_in_event", new=AsyncMock(return_value=False)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock(return_value=True)), \
         patch.object(client, "ensure_dj_page", new=AsyncMock()) as ensure:
        await view._handle_confirm(inter)
    ensure.assert_not_awaited()
    for method in (client._client.request, client._client.get, client._client.post,
                   client._client.patch):
        method.assert_not_called()
    log.post_error.assert_not_awaited()
    assert "canceled" in inter.response.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_page_id_from_before_the_dj_database_is_replaced() -> None:
    """Story 8.5 review: an old page in the events database would make the link fail."""
    ctx = _classic_booking_ctx(40007, ensure_returns="pg-in-dj-db")
    ctx.notion_client.separate_dj_database = True
    ctx.notion_client.dj_page_in_dj_database = AsyncMock(return_value=False)
    _, rel, stored, _ = await _confirm_classic(ctx, 40007, page_id="pg-old")
    ctx.notion_client.dj_page_in_dj_database.assert_awaited_once_with("pg-old")
    ctx.notion_client.ensure_dj_page.assert_awaited_once_with(40007, "DJ Nova", "Test Rave")
    assert ctx.notion_client.write_booking.call_args.kwargs["dj_notion_page_id"] == "pg-in-dj-db"
    stored.assert_awaited_once_with(ctx.db, 40007, "pg-in-dj-db")
    rel.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_id_in_the_dj_database_is_kept() -> None:
    ctx = _classic_booking_ctx(40008, ensure_returns="unused")
    ctx.notion_client.separate_dj_database = True
    ctx.notion_client.dj_page_in_dj_database = AsyncMock(return_value=True)
    await _confirm_classic(ctx, 40008, page_id="pg-ok")
    ctx.notion_client.ensure_dj_page.assert_not_awaited()
    assert ctx.notion_client.write_booking.call_args.kwargs["dj_notion_page_id"] == "pg-ok"
