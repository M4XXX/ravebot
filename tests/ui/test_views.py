"""Tests for bot/ui/views.py (menu, booking, cancellation, outreach views)."""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.notion.models import EventInfo
from bot.ui import texts
from tests.conftest import make_real_ctx
from tests.notion.factories import make_event, make_slot

# ---------------------------------------------------------------------------
# Story 2.4: pass_for_now + FallbackMenuView
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pass_for_now_sends_warm_response() -> None:
    """pass_for_now sends warm decline; no DB write, no lock acquired."""
    from bot.ui.dj_profile import OnboardingIntroView

    ctx = make_real_ctx(db=MagicMock())
    view = OnboardingIntroView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 12345
    inter.response.send_message = AsyncMock()

    await view.pass_for_now.callback(inter)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert msg == texts.ONBOARD_DECLINED
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
    assert msg == texts.INVITES_ON_DONE
    member.add_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_go_active_dj_not_in_db() -> None:
    """go_active responds with onboarding prompt when DJ has no DB record."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 88888
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.client.guilds = []

    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=False)):
        await view.go_active.callback(inter)

    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once()
    assert inter.followup.send.call_args[0][0] == texts.NO_PROFILE


def _menu_inter(user_id: int, guild: MagicMock) -> MagicMock:
    from types import SimpleNamespace

    inter = MagicMock()
    inter.author.id = user_id
    inter.client.guilds = [guild]
    done = {"v": False}
    inter.response.is_done = MagicMock(side_effect=lambda: done["v"])

    async def _defer(*args: object, **kwargs: object) -> None:
        done["v"] = True

    inter.response.defer = AsyncMock(side_effect=_defer)
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock(return_value=SimpleNamespace(id=321))
    inter.original_response = AsyncMock(return_value=SimpleNamespace(id=321))
    return inter


def _service_ctx() -> object:
    from bot.services.dj_conversation import Reply

    ctx = make_real_ctx(db=MagicMock())
    service = MagicMock()
    service.start_onboarding = AsyncMock(
        return_value=Reply(texts.Q_NAME, "question", step="name")
    )
    service.show_overview = AsyncMock(
        return_value=Reply(texts.OVERVIEW_TITLE, "overview", in_group=True)
    )
    service.record_message_id = AsyncMock()
    ctx.dj_conversations = service
    return ctx


def _guild_with_member(roles: list) -> tuple[MagicMock, MagicMock]:
    member = MagicMock(spec=disnake.Member)
    member.roles = roles
    guild = MagicMock(spec=disnake.Guild)
    guild.id = 99999
    guild.get_member.return_value = member
    return guild, member


@pytest.mark.asyncio
async def test_fallback_go_active_without_row_and_without_dj_role_gets_no_profile() -> None:
    from bot.ui.views import FallbackMenuView

    ctx = _service_ctx()
    dj_role = MagicMock(spec=disnake.Role)
    guild, _ = _guild_with_member([])
    inter = _menu_inter(88888, guild)

    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=False)), \
         patch("bot.ui.views.get_dj_role", new=AsyncMock(return_value=dj_role)):
        await FallbackMenuView(ctx).go_active.callback(inter)

    inter.response.defer.assert_awaited_once()
    inter.response.send_message.assert_not_called()
    assert inter.followup.send.call_args[0][0] == texts.NO_PROFILE
    assert inter.followup.send.call_args.kwargs["ephemeral"] is True
    ctx.dj_conversations.start_onboarding.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_go_active_without_row_with_dj_role_starts_onboarding() -> None:
    from bot.ui.dj_profile import QuestionView
    from bot.ui.views import FallbackMenuView

    ctx = _service_ctx()
    dj_role = MagicMock(spec=disnake.Role)
    guild, _ = _guild_with_member([dj_role])
    inter = _menu_inter(88888, guild)

    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=False)), \
         patch("bot.ui.views.get_dj_role", new=AsyncMock(return_value=dj_role)):
        await FallbackMenuView(ctx).go_active.callback(inter)

    ctx.dj_conversations.start_onboarding.assert_awaited_once_with(88888, 99999)
    inter.response.defer.assert_awaited_once()
    inter.response.send_message.assert_not_called()
    sent = inter.followup.send.call_args
    assert sent.args[0] == texts.Q_NAME
    assert isinstance(sent.kwargs["view"], QuestionView)
    ctx.dj_conversations.record_message_id.assert_awaited_once_with(88888, 321)


@pytest.mark.asyncio
async def test_menu_my_profile_only_for_onboarded_djs() -> None:
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    for active in (None, False, True):
        with_profile = [c.custom_id for c in FallbackMenuView(ctx, active, onboarded=True).children]
        without = [c.custom_id for c in FallbackMenuView(ctx, active, onboarded=False).children]
        assert "fallback:my_profile" in with_profile
        assert "fallback:my_profile" not in without
    button = next(
        c for c in FallbackMenuView(ctx, True, onboarded=True).children
        if c.custom_id == "fallback:my_profile"
    )
    assert (button.label, button.row) == (texts.BTN_MY_PROFILE, 2)


@pytest.mark.asyncio
async def test_menu_my_profile_sends_the_overview() -> None:
    from bot.ui.dj_profile import OverviewView
    from bot.ui.views import FallbackMenuView

    ctx = _service_ctx()
    guild, _ = _guild_with_member([])
    inter = _menu_inter(4242, guild)

    await FallbackMenuView(ctx, True, onboarded=True).my_profile.callback(inter)

    ctx.dj_conversations.show_overview.assert_awaited_once_with(4242)
    sent = inter.response.send_message.call_args
    assert sent.args[0] == texts.OVERVIEW_TITLE
    assert isinstance(sent.kwargs["view"], OverviewView)
    assert "dj:leave" in [c.custom_id for c in sent.kwargs["view"].children]
    ctx.dj_conversations.record_message_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistent_menu_has_every_button() -> None:
    from bot.ui.views import FallbackMenuView

    view = FallbackMenuView(make_real_ctx(db=MagicMock()), all_buttons=True)
    assert sorted(c.custom_id for c in view.children) == sorted([
        "fallback:book_slot", "fallback:cancel_slot", "fallback:go_active",
        "fallback:go_inactive", "fallback:show_lineup", "fallback:my_profile",
    ])
    assert view.is_persistent()


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
    assert msg == texts.INVITES_OFF_DONE
    member.remove_roles.assert_awaited_once()


# ---------------------------------------------------------------------------
# Story 3.3: FallbackMenuView.show_lineup()
# ---------------------------------------------------------------------------

def _make_notion_event(
    name: str = "Friday Night Rave",
    slots: int = 3,
    djs: int = 1,
) -> EventInfo:
    return make_event(
        event_id=name,
        page_id="page-uuid",
        slot_count=slots,
        slots=[make_slot(i + 1, dj_page_id=f"dj-{i}", dj_name=f"DJ {i}") for i in range(djs)],
    )


@pytest.mark.asyncio
async def test_show_lineup_sends_embed_for_first_event() -> None:
    """Events returned → LineupUpdateEmbed sent for first event."""
    from bot.ui.embeds import LineupUpdateEmbed
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(return_value=[_lineup_event("Friday Night Rave", 2)])
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in call_kwargs
    assert isinstance(call_kwargs["embed"], LineupUpdateEmbed)


@pytest.mark.asyncio
async def test_show_lineup_sends_no_events_message_when_empty() -> None:
    """Empty events list → 'No upcoming events' message sent."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert msg == texts.NO_LINEUP


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
    assert msg == texts.GENERIC_ERROR


@pytest.mark.asyncio
async def test_show_lineup_handles_poll_exception() -> None:
    """list_events raises → 'Something went wrong' message sent, no crash."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(side_effect=Exception("Notion down"))
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    inter.response.send_message.assert_awaited_once()
    call_args = inter.response.send_message.call_args
    msg = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert msg == texts.GENERIC_ERROR


def _lineup_event(event_id: str, day: int):
    import datetime as _dt

    future = _dt.datetime.now(_dt.UTC).date() + _dt.timedelta(days=day)
    return make_event(event_id=event_id, page_id=f"p-{event_id}", date=future)


@pytest.mark.asyncio
async def test_show_lineup_single_event_has_no_pick_view() -> None:
    from bot.ui.embeds import LineupUpdateEmbed
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[_lineup_event("E1", 3)])
    view = FallbackMenuView(ctx)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    kw = inter.response.send_message.call_args.kwargs
    assert isinstance(kw["embed"], LineupUpdateEmbed)
    assert kw["ephemeral"] is True
    assert kw.get("view") is None


@pytest.mark.asyncio
async def test_show_lineup_multi_event_offers_picker_capped_at_five() -> None:
    from bot.ui.views import FallbackMenuView, LineupEventPickView

    events = [_lineup_event(f"E{i}", i + 1) for i in range(7)]
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=events)
    view = FallbackMenuView(ctx)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    kw = inter.response.send_message.call_args.kwargs
    pick = kw["view"]
    assert isinstance(pick, LineupEventPickView)
    assert [c.label for c in pick.children] == ["E0", "E1", "E2", "E3", "E4"]
    assert kw["embed"].footer.text == "E0"

    # tapping a button replies privately with that event's lineup
    inter2 = MagicMock(spec=disnake.MessageInteraction)
    inter2.response.send_message = AsyncMock()
    await pick.children[2].callback(inter2)
    kw2 = inter2.response.send_message.call_args.kwargs
    assert kw2["embed"].footer.text == "E2"
    assert kw2["ephemeral"] is True


@pytest.mark.asyncio
async def test_show_lineup_ignores_past_events() -> None:
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[_lineup_event("OLD", -3)])
    view = FallbackMenuView(ctx)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.show_lineup.callback(inter)

    assert inter.response.send_message.call_args[0][0] == texts.NO_LINEUP


def _menu_labels(view) -> list[str]:
    return [c.label for c in view.children]


@pytest.mark.asyncio
async def test_menu_buttons_by_dj_state() -> None:
    from bot.ui.views import FallbackMenuView

    base = [texts.BTN_BOOK, texts.BTN_CANCEL, texts.BTN_LINEUP]
    ctx = make_real_ctx(db=MagicMock())
    for state, pool in ((None, texts.BTN_INVITES_ON), (False, texts.BTN_INVITES_ON),
                        (True, texts.BTN_INVITES_OFF)):
        labels = _menu_labels(FallbackMenuView(ctx, state))
        assert sorted(labels) == sorted([*base, pool])
    assert _menu_labels(FallbackMenuView(ctx)).count(texts.BTN_INVITES_OFF) == 0


# ---------------------------------------------------------------------------
# Story 4.1: FallbackMenuView.book_slot + EventSelectionView + SlotSelectionView
# ---------------------------------------------------------------------------

def _make_event(
    event_id: str = "evt-001",
    name: str = "Test Rave",
    slots: int = 6,
    djs_booked: int = 2,
) -> EventInfo:
    """Helper: typed event; event_id is the Notion page id, the first djs_booked slots are held."""
    return make_event(
        event_id=name,
        page_id=event_id,
        date=datetime.date(2026, 7, 1),
        start=datetime.time(20, 0),
        slot_count=slots,
        slots=[
            make_slot(i + 1, dj_page_id=f"dj-{i}", dj_name=f"DJ {i}")
            for i in range(djs_booked)
        ],
    )


def _slot_button_indices(view: object) -> list[int]:
    """Slot indices offered by a SlotSelectionView (custom_id slot_select:<page>:<index>)."""
    return [
        int(item.custom_id.rsplit(":", 1)[1])
        for item in view.children  # type: ignore[attr-defined]
        if item.custom_id.startswith("slot_select:") and item.custom_id != "slot_select:cancel"
    ]


@pytest.mark.asyncio
async def test_book_slot_lock_already_held_returns_message() -> None:
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
async def test_book_slot_notion_client_none_releases_lock() -> None:
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
async def test_book_slot_list_events_raises_releases_lock() -> None:
    """list_events raises → error message sent, lock released. (AC6)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(side_effect=Exception("Notion down"))
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 33333
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    assert not ctx.get_user_lock(33333).locked()  # Lock released


@pytest.mark.asyncio
async def test_book_slot_all_events_full_releases_lock() -> None:
    """All events fully booked → 'All slots filled' message, lock released. (AC3)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    # Event with all 6 slots booked
    notion_client.list_events = AsyncMock(return_value=[_make_event(slots=6, djs_booked=6)])
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
async def test_book_slot_no_events_at_all_releases_lock() -> None:
    """list_events returns [] → 'All slots filled' message, lock released. (AC3)"""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client
    view = FallbackMenuView(ctx)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 55555
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    inter.response.send_message.assert_awaited_once()
    assert not ctx.get_user_lock(55555).locked()


@pytest.mark.asyncio
async def test_book_slot_open_slots_shows_event_selection_view() -> None:
    """Events with open slots → message with EventSelectionView sent. (AC1)"""
    from bot.ui.views import EventSelectionView, FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    notion_client = MagicMock()
    notion_client.list_events = AsyncMock(
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
    content = call_args[0][0] if call_args[0] else call_kwargs.get("content", "")
    assert "1. Test Rave · " in content and "4 slots open" in content
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
    """Event button tapped → PICK_SLOT + LineupUpdateEmbed + SlotSelectionView sent. (AC2)"""
    from bot.ui.embeds import LineupUpdateEmbed
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
    assert isinstance(call_kwargs["embed"], LineupUpdateEmbed)
    assert call_kwargs["content"] == texts.PICK_SLOT
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
    ctx.notion_client.list_events = AsyncMock(return_value=[])  # Story 4.3: poll called in slot-taken path
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
    """NotionWriteError on assign_slot → error msg, slot lock and per-user lock released."""
    from bot.notion.client import NotionWriteError

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20004)
    await lock.acquire()

    notion_client = MagicMock()
    notion_client.assign_slot = AsyncMock(side_effect=NotionWriteError("fail"))
    ctx.notion_client = notion_client

    view = _make_booking_confirm_view(ctx, lock, user_id=20004)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()) as mock_release, \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-page")):
        await view._handle_confirm(inter)

    mock_release.assert_awaited_once()
    assert mock_release.await_args.kwargs == {"expected_holder": 20004}
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "something went wrong" in msg.lower()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_success_sends_confirmed_embed() -> None:
    """Full success path → BookedEmbed sent, per-user lock released. (AC3)"""
    from bot.ui.embeds import BookedEmbed

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20005)
    await lock.acquire()

    notion_client = MagicMock()
    notion_client.assign_slot = AsyncMock(return_value=True)
    notion_client.list_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client

    view = _make_booking_confirm_view(ctx, lock, user_id=20005)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="notion-dj-001")):
        await view._handle_confirm(inter)

    inter.response.send_message.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in call_kwargs
    assert isinstance(call_kwargs["embed"], BookedEmbed)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_success_triggers_lineup_post() -> None:
    """Success path with lineup_service set → notify_changed called once. (AC3)"""
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20006)
    await lock.acquire()

    fresh_events = [_make_event()]
    notion_client = MagicMock()
    notion_client.assign_slot = AsyncMock(return_value=True)
    notion_client.list_events = AsyncMock(return_value=fresh_events)
    ctx.notion_client = notion_client

    lineup_service = MagicMock()
    ctx.lineup_service = lineup_service

    view = _make_booking_confirm_view(ctx, lock, user_id=20006)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-page")):
        await view._handle_confirm(inter)

    lineup_service.notify_changed.assert_called_once_with()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_success_posts_admin_log() -> None:
    """Success path → AdminLogService.log_booked called once with the DJ name, via DM. (AC3)"""
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20007)
    await lock.acquire()

    notion_client = MagicMock()
    notion_client.assign_slot = AsyncMock(return_value=True)
    notion_client.list_events = AsyncMock(return_value=[])
    ctx.notion_client = notion_client

    log_service = MagicMock()
    log_service.log_booked = AsyncMock()
    ctx.log_service = log_service

    view = _make_booking_confirm_view(ctx, lock, user_id=20007, slot_index=2)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.display_name = "Nova"
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-page")):
        await view._handle_confirm(inter)

    log_service.log_booked.assert_awaited_once_with("Nova", view._event, 2, "DM")
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
    ctx.notion_client.list_events = AsyncMock(return_value=[])
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
    """acquire_slot_lock returns False → list_events called, SlotSelectionView uses fresh event. (AC1)"""
    from bot.ui.views import SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    # Return a fresh event with the same ID but updated data (8 slots instead of 6)
    fresh_event = _make_event(event_id="evt-001", name="Fresh Rave", slots=8, djs_booked=2)
    ctx.notion_client.list_events = AsyncMock(return_value=[fresh_event])
    lock = ctx.get_user_lock(20010)
    await lock.acquire()
    view = _make_booking_confirm_view(ctx, lock, user_id=20010)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=False)):
        await view._handle_confirm(inter)

    ctx.notion_client.list_events.assert_awaited_once()
    call_kwargs = inter.response.send_message.call_args.kwargs
    presented_view = call_kwargs.get("view")
    assert isinstance(presented_view, SlotSelectionView)
    assert presented_view._event is fresh_event  # fresh data used, not stale self._event
    msg = inter.response.send_message.call_args[0][0]
    assert msg == texts.SLOT_TAKEN


@pytest.mark.asyncio
async def test_booking_confirm_slot_conflict_all_slots_taken() -> None:
    """acquire_slot_lock returns False and no slots remain → lock released, 'fully booked' message."""
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    # All 6 slots booked — remaining will be [] after excluding the conflicting slot_index=2
    ctx.notion_client.list_events = AsyncMock(
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
    assert msg == texts.EVENT_FULL


@pytest.mark.asyncio
async def test_booking_confirm_pg_failure_releases_lock() -> None:
    """acquire_slot_lock raises Exception → lock released, error message, Notion not written. (AC3)"""
    ctx = make_real_ctx(db=MagicMock())
    # notion_client must NOT be None — the Notion guard runs before acquire_slot_lock
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = AsyncMock(return_value=True)
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
    ctx.notion_client.assign_slot.assert_not_called()  # Notion NOT written


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
    ctx.notion_client.list_events = AsyncMock(return_value=[])
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
    ctx.notion_client.list_events = AsyncMock(return_value=[event])
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
async def test_cancel_confirm_confirm_calls_clear_slot() -> None:
    """Confirm tapped → clear_slot called, release_slot_lock called, confirmed message. (AC4)"""
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(
        return_value=[_make_event(event_id="evt-cancel-04", djs_booked=1)]
    )
    ctx.notion_client.clear_slot = AsyncMock(return_value=True)
    ctx.log_service = None
    ctx.lineup_service = None
    lock = ctx.get_user_lock(30006)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-04", djs_booked=1)
    view = CancelConfirmView(ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=30006)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()

    with patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="notion-page-dj")), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()) as mock_release:
        await view._handle_confirm(inter)

    ctx.notion_client.clear_slot.assert_awaited_once_with(event, 0, "notion-page-dj")
    mock_release.assert_awaited_once_with(ctx.db, "evt-cancel-04", 0, expected_holder=30006)
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "cancelled" in msg.lower()
    assert not lock.locked()  # lock released


@pytest.mark.asyncio
async def test_cancel_confirm_notion_write_error_releases_lock() -> None:
    """clear_slot raises NotionWriteError → error message, release_slot_lock called, lock released."""
    from bot.notion.client import NotionWriteError
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(
        return_value=[_make_event(event_id="evt-cancel-05", djs_booked=1)]
    )
    ctx.notion_client.clear_slot = AsyncMock(side_effect=NotionWriteError("write failed"))
    ctx.log_service = None
    ctx.lineup_service = None
    lock = ctx.get_user_lock(30007)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-05", djs_booked=1)
    view = CancelConfirmView(ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=30007)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()

    with patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="notion-page-dj")), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()) as mock_release:
        await view._handle_confirm(inter)

    # Even on Notion failure, slot lock must be freed
    mock_release.assert_awaited_once_with(ctx.db, "evt-cancel-05", 0, expected_holder=30007)
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args[0][0]
    assert "something went wrong" in msg.lower() or "try again" in msg.lower()
    assert not lock.locked()  # lock released


@pytest.mark.asyncio
async def test_cancel_slot_list_events_failure_releases_lock() -> None:
    """list_events raises after slot_lock_rows found → error message, lock released."""
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(side_effect=RuntimeError("network error"))
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
    # list_events returns an event with a DIFFERENT id — no match for the slot_lock row
    ctx.notion_client.list_events = AsyncMock(
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
# Notion rework: views read typed EventInfo (open slots from slot rows)
# ---------------------------------------------------------------------------

def _gap_event(page_id: str = "evt-gap") -> EventInfo:
    """3-slot event: slots 1 and 3 held, slot 2 (index 1) open."""
    return make_event(
        event_id="Gap Rave",
        page_id=page_id,
        slot_count=3,
        slots=[
            make_slot(1, dj_page_id="dj-a", dj_name="A"),
            make_slot(2, dj_page_id=None),
            make_slot(3, dj_page_id="dj-b", dj_name="B"),
        ],
    )


def test_filter_drops_full_event_and_keeps_event_with_gap() -> None:
    from bot.ui.views import _filter_events_with_open_slots

    full = _make_event(event_id="evt-full", slots=2, djs_booked=2)
    gap = _gap_event()
    assert _filter_events_with_open_slots([full, gap]) == [gap]


def test_filter_dj_with_two_slots_does_not_open_the_event() -> None:
    """One DJ holding both slots of a 2-slot event: the event is full."""
    from bot.ui.views import _filter_events_with_open_slots

    event = make_event(
        page_id="evt-double",
        slot_count=2,
        slots=[
            make_slot(1, dj_page_id="dj-a", dj_name="A"),
            make_slot(2, dj_page_id="dj-a", dj_name="A"),
        ],
    )
    assert _filter_events_with_open_slots([event]) == []


def test_filter_missing_slot_rows_count_as_open() -> None:
    from bot.ui.views import _filter_events_with_open_slots

    event = make_event(page_id="evt-norows", slot_count=2, slots=[])
    assert _filter_events_with_open_slots([event]) == [event]


@pytest.mark.asyncio
async def test_event_selection_offers_exactly_the_open_gap_slot() -> None:
    from bot.ui.views import EventSelectionView, SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(40001)
    await lock.acquire()
    event = _gap_event()
    view = EventSelectionView(ctx, [event], lock, 40001)

    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()
    await view._handle_event(inter, event)

    slot_view = inter.response.send_message.call_args.kwargs["view"]
    assert isinstance(slot_view, SlotSelectionView)
    assert _slot_button_indices(slot_view) == [1]
    labels = [i.label for i in slot_view.children if i.custom_id.endswith(":1")]
    assert labels == [texts.slot_button(event, 1)]
    assert slot_view.children[0].custom_id == "slot_select:evt-gap:1"


@pytest.mark.asyncio
async def test_event_selection_dj_with_two_slots_leaves_only_real_gaps() -> None:
    """DJ holding slots 1 and 2 of a 4-slot event → only indices 2 and 3 offered."""
    from bot.ui.views import EventSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(40002)
    await lock.acquire()
    event = make_event(
        page_id="evt-two",
        slot_count=4,
        slots=[
            make_slot(1, dj_page_id="dj-a", dj_name="A"),
            make_slot(2, dj_page_id="dj-a", dj_name="A"),
        ],
    )
    view = EventSelectionView(ctx, [event], lock, 40002)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()
    await view._handle_event(inter, event)

    assert _slot_button_indices(inter.response.send_message.call_args.kwargs["view"]) == [2, 3]


@pytest.mark.asyncio
async def test_event_selection_button_uses_event_id_and_page_id() -> None:
    from bot.ui.views import EventSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(40003)
    await lock.acquire()
    view = EventSelectionView(ctx, [_gap_event()], lock, 40003)
    first = view.children[0]
    assert first.label == "Gap Rave"
    assert first.custom_id == "event_select:evt-gap"


@pytest.mark.asyncio
async def test_book_slot_lists_event_name_and_start_timestamp() -> None:
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    event = _gap_event()
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[event])
    view = FallbackMenuView(ctx)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 40004
    inter.response.send_message = AsyncMock()

    await view.book_slot.callback(inter)

    content = inter.response.send_message.call_args[0][0]
    assert content.splitlines()[1].startswith("1. Gap Rave · ")


@pytest.mark.asyncio
async def test_slot_selection_slot_time_uses_slot_length() -> None:
    from bot.ui.views import BookingConfirmView, SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(40005)
    await lock.acquire()
    event = make_event(page_id="evt-45", slot_count=4, slot_minutes=45)
    view = SlotSelectionView(ctx, event, [0, 1, 2, 3], lock, 40005)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view._handle_slot(inter, 2)

    confirm = inter.response.send_message.call_args.kwargs["view"]
    assert isinstance(confirm, BookingConfirmView)
    assert confirm._slot_unix == int(event.slot_start(2).timestamp())
    assert confirm._event is event


@pytest.mark.asyncio
async def test_booking_confirm_lost_lock_lists_open_slots_from_fresh_event() -> None:
    """Stale: slot 1 held. Fresh: slots 1, 2, 4 held → only index 2 remains (index 1 excluded)."""
    from bot.ui.views import BookingConfirmView, SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    stale = make_event(
        page_id="evt-race", slot_count=4, slots=[make_slot(1, dj_page_id="dj-a")]
    )
    fresh = make_event(
        page_id="evt-race",
        slot_count=4,
        slots=[
            make_slot(1, dj_page_id="dj-a"),
            make_slot(2, dj_page_id="dj-winner"),
            make_slot(4, dj_page_id="dj-c"),
        ],
    )
    other = make_event(page_id="evt-other", slot_count=4)
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[other, fresh])
    lock = ctx.get_user_lock(40006)
    await lock.acquire()
    view = BookingConfirmView(
        ctx=ctx, event=stale, slot_index=1, slot_unix=0, lock=lock, user_id=40006
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=False)) as acq:
        await view._handle_confirm(inter)

    assert acq.call_args.args[1] == "evt-race"
    presented = inter.response.send_message.call_args.kwargs["view"]
    assert isinstance(presented, SlotSelectionView)
    assert presented._event is fresh
    assert _slot_button_indices(presented) == [2]
    assert lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_assigns_slot_with_dj_page_id() -> None:
    from bot.ui.views import BookingConfirmView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(40007)
    await lock.acquire()
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = AsyncMock(return_value=True)
    ctx.notion_client.list_events = AsyncMock(return_value=[])
    event = _gap_event()
    view = BookingConfirmView(
        ctx=ctx, event=event, slot_index=1, slot_unix=1751400000, lock=lock, user_id=40007
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.display_name = "Disp"
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)) as acq, \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-me")) as ens:
        await view._handle_confirm(inter)

    assert acq.call_args.args[1:3] == ("evt-gap", 1)
    ens.assert_awaited_once_with(ctx.db, ctx.notion_client, 40007, "Disp")
    ctx.notion_client.assign_slot.assert_awaited_once_with(event, 1, "dj-me")
    assert not lock.locked()


async def _run_booking(ctx, user_id, *, ensure, assign=None, lock_ok=True):
    """Drive BookingConfirmView._handle_confirm; return (inter, lock, release mock)."""
    from bot.ui.views import BookingConfirmView

    lock = ctx.get_user_lock(user_id)
    await lock.acquire()
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = assign or AsyncMock(return_value=True)
    fresh = make_event(
        event_id="Gap Rave", page_id="evt-gap", slot_count=4,
        slots=[make_slot(1, dj_page_id="dj-a"), make_slot(2, dj_page_id="dj-x"),
               make_slot(3, dj_page_id="dj-b")],
    )
    ctx.notion_client.list_events = AsyncMock(return_value=[fresh])
    ctx.log_service = None
    ctx.lineup_service = None
    view = BookingConfirmView(
        ctx=ctx, event=_gap_event(), slot_index=1, slot_unix=1751400000, lock=lock,
        user_id=user_id,
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=lock_ok)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()) as rel, \
         patch("bot.services.dj_pages.ensure_dj_page", new=ensure):
        await view._handle_confirm(inter)
    return inter, lock, rel


@pytest.mark.asyncio
async def test_booking_confirm_assign_false_releases_pg_lock_and_refreshes() -> None:
    from bot.ui.views import SlotSelectionView

    ctx = make_real_ctx(db=MagicMock())
    inter, lock, rel = await _run_booking(
        ctx, 40020, ensure=AsyncMock(return_value="dj-me"),
        assign=AsyncMock(return_value=False),
    )
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 1, expected_holder=40020)
    msg = inter.response.send_message.call_args[0][0]
    assert "just taken" in msg
    assert isinstance(inter.response.send_message.call_args.kwargs["view"], SlotSelectionView)
    assert lock.locked()  # held by the refreshed slot selection


@pytest.mark.asyncio
async def test_booking_confirm_assign_false_logs_slot_conflict() -> None:
    from bot.ui.embeds import ErrorCode

    ctx = make_real_ctx(db=MagicMock())
    log = MagicMock()
    log.post_error = AsyncMock()
    lock = ctx.get_user_lock(40021)
    await lock.acquire()
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = AsyncMock(return_value=False)
    ctx.notion_client.list_events = AsyncMock(return_value=[_gap_event()])
    ctx.log_service = log
    from bot.ui.views import BookingConfirmView
    view = BookingConfirmView(
        ctx=ctx, event=_gap_event(), slot_index=1, slot_unix=1, lock=lock, user_id=40021
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()), \
         patch("bot.db.bookings.get_slot_lock_holder", new=AsyncMock(return_value=None)), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-me")):
        await view._handle_confirm(inter)
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_SLOT_CONFLICT


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_name", ["NotionWriteError", "SlotRowMissingError"])
async def test_booking_confirm_assign_errors_release_both_locks(exc_name) -> None:
    import bot.notion.client as nc

    exc = (
        nc.NotionWriteError("x")
        if exc_name == "NotionWriteError"
        else nc.SlotRowMissingError("e", 2)
    )
    ctx = make_real_ctx(db=MagicMock())
    inter, lock, rel = await _run_booking(
        ctx, 40022, ensure=AsyncMock(return_value="dj-me"), assign=AsyncMock(side_effect=exc)
    )
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 1, expected_holder=40022)
    assert inter.response.send_message.call_args[0][0] == texts.GENERIC_ERROR
    assert not lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ensure", [AsyncMock(return_value=None), AsyncMock(side_effect=RuntimeError("boom"))]
)
async def test_booking_confirm_no_dj_page_releases_both_locks(ensure) -> None:
    ctx = make_real_ctx(db=MagicMock())
    inter, lock, rel = await _run_booking(ctx, 40023, ensure=ensure)
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 1, expected_holder=40023)
    ctx.notion_client.assign_slot.assert_not_called()
    assert inter.response.send_message.call_args[0][0] == texts.GENERIC_ERROR
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancel_slot_matches_lock_rows_by_page_id_and_slot_time() -> None:
    from bot.ui.views import CancellationView, FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    event = make_event(page_id="evt-c", event_id="Cancel Rave", slot_minutes=30)
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[event])
    view = FallbackMenuView(ctx)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 40008
    inter.response.send_message = AsyncMock()

    rows = [{"event_id": "evt-c", "slot_index": 3}]
    with patch("bot.db.bookings.get_dj_slot_locks", new=AsyncMock(return_value=rows)), \
         patch("bot.ui.views.CancellationView", wraps=CancellationView) as cv:
        await view.cancel_slot.callback(inter)

    bookings = cv.call_args.args[1]
    assert bookings == [
        {"event": event, "slot_index": 3, "slot_unix": int(event.slot_start(3).timestamp())}
    ]
    label = inter.response.send_message.call_args.kwargs["view"].children[0].label
    assert label == "Cancel Rave · Slot 4 · 21:30"


from bot.notion.client import SlotRowMissingError  # noqa: E402


async def _run_cancel(user_id, *, ensure, clear, lineup=None):
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    event = _gap_event()
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[event])
    ctx.notion_client.clear_slot = clear
    ctx.log_service = None
    ctx.lineup_service = lineup
    lock = ctx.get_user_lock(user_id)
    await lock.acquire()
    view = CancelConfirmView(
        ctx, event=event, slot_index=2, slot_unix=1751414400, lock=lock, user_id=user_id
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.display_name = "Disp"
    inter.response.send_message = AsyncMock()
    with patch("bot.services.dj_pages.ensure_dj_page", new=ensure), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()) as rel:
        await view._handle_confirm(inter)
    return ctx, event, inter, lock, rel


@pytest.mark.asyncio
async def test_cancel_confirm_clears_slot_with_dj_page_id() -> None:
    clear = AsyncMock(return_value=True)
    ensure = AsyncMock(return_value="dj-me")
    ctx, event, inter, lock, rel = await _run_cancel(40009, ensure=ensure, clear=clear)
    ensure.assert_awaited_once_with(ctx.db, ctx.notion_client, 40009, "Disp")
    clear.assert_awaited_once_with(event, 2, "dj-me")
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 2, expected_holder=40009)
    assert "cancelled" in inter.response.send_message.call_args[0][0]
    assert not lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ensure", "clear"),
    [
        (AsyncMock(return_value="dj-me"), AsyncMock(return_value=False)),
        (AsyncMock(return_value="dj-me"), AsyncMock(side_effect=SlotRowMissingError("e", 3))),
        (AsyncMock(return_value=None), AsyncMock(return_value=True)),
        (AsyncMock(side_effect=RuntimeError("boom")), AsyncMock(return_value=True)),
    ],
)
async def test_cancel_confirm_always_releases_pg_lock(ensure, clear) -> None:
    ctx, event, inter, lock, rel = await _run_cancel(40030, ensure=ensure, clear=clear)
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 2, expected_holder=40030)
    assert not lock.locked()
    inter.response.send_message.assert_awaited_once()
    if ensure.await_args is not None and ensure.return_value is None:
        clear.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_confirm_notion_write_error_still_releases_both_locks() -> None:
    from bot.notion.client import NotionWriteError

    clear = AsyncMock(side_effect=NotionWriteError("x"))
    ctx, event, inter, lock, rel = await _run_cancel(
        40031, ensure=AsyncMock(return_value="dj-me"), clear=clear
    )
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 2, expected_holder=40031)
    assert inter.response.send_message.call_args[0][0] == texts.GENERIC_ERROR
    assert not lock.locked()


@pytest.mark.asyncio
async def test_outreach_accept_offers_open_slots_from_fresh_event() -> None:
    from bot.ui.views import OutreachView, SlotSelectionView

    ctx = make_real_ctx(db=None)
    stale = make_event(page_id="evt-o", slot_count=3)
    fresh = make_event(
        page_id="evt-o", slot_count=3, slots=[make_slot(2, dj_page_id="dj-x")]
    )
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[fresh])
    view = OutreachView(ctx=ctx, event=stale, discord_user_id=40010)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 40010
    inter.response.send_message = AsyncMock()

    await view.accept.callback(inter)

    slot_view = inter.response.send_message.call_args.kwargs["view"]
    assert isinstance(slot_view, SlotSelectionView)
    assert slot_view._event is fresh
    assert _slot_button_indices(slot_view) == [0, 2]


@pytest.mark.asyncio
async def test_outreach_accept_full_event_releases_lock() -> None:
    from bot.ui.views import OutreachView

    ctx = make_real_ctx(db=None)
    full = _make_event(event_id="evt-of", slots=2, djs_booked=2)
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[full])
    view = OutreachView(ctx=ctx, event=full, discord_user_id=40011)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 40011
    inter.response.send_message = AsyncMock()

    await view.accept.callback(inter)

    assert inter.response.send_message.call_args[0][0] == texts.INVITE_FILLED
    assert not ctx.get_user_lock(40011).locked()


@pytest.mark.asyncio
async def test_outreach_view_button_labels() -> None:
    from bot.ui.views import OutreachView

    view = OutreachView(ctx=make_real_ctx(db=None), event=make_event(), discord_user_id=1)
    assert [c.label for c in view.children] == ["Pick a Slot", "Not this time"]


@pytest.mark.asyncio
async def test_outreach_pick_slot_sends_lineup_and_open_slot_buttons() -> None:
    from bot.ui.embeds import LineupUpdateEmbed
    from bot.ui.views import OutreachView

    ctx = make_real_ctx(db=None)
    event = make_event(
        event_id="Rave", page_id="evt-p", slot_count=4,
        slots=[make_slot(2, dj_page_id="dj-x", dj_name="Booked Bob")],
    )
    ctx.notion_client = MagicMock()
    ctx.notion_client.list_events = AsyncMock(return_value=[event])
    view = OutreachView(ctx=ctx, event=event, discord_user_id=40050)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 40050
    inter.response.send_message = AsyncMock()

    await view.accept.callback(inter)

    kwargs = inter.response.send_message.call_args.kwargs
    assert kwargs["content"] == texts.PICK_SLOT
    assert isinstance(kwargs["embed"], LineupUpdateEmbed)
    assert "Booked Bob" in kwargs["embed"].description
    assert "OPEN SLOT" in kwargs["embed"].description
    labels = [c.label for c in kwargs["view"].children]
    assert labels == [
        texts.slot_button(event, 0),
        texts.slot_button(event, 2),
        texts.slot_button(event, 3),
        texts.BTN_NEVER_MIND,
    ]
    assert labels[0].startswith("Slot 1 · ")


@pytest.mark.asyncio
async def test_slot_selection_view_caps_at_discord_component_limit() -> None:
    from bot.ui.views import SlotSelectionView

    ctx = make_real_ctx(db=None)
    event = make_event(page_id="evt-big", slot_count=40)
    view = SlotSelectionView(ctx, event, list(range(40)), ctx.get_user_lock(1), 1)
    assert len(_slot_button_indices(view)) == 24
    assert view.children[-1].label == texts.BTN_NEVER_MIND
    assert len(view.children) == 25  # Discord allows 25 components


@pytest.mark.asyncio
async def test_outreach_decline_replies_invite_declined() -> None:
    from bot.ui.views import OutreachView

    ctx = make_real_ctx(db=None)
    view = OutreachView(ctx=ctx, event=make_event(), discord_user_id=40051)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()

    await view.decline.callback(inter)

    assert inter.response.send_message.call_args[0][0] == texts.INVITE_DECLINED


# ---------------------------------------------------------------------------
# Review fix round 1: lock release on every exit path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [KeyError("k"), TypeError("t"), RuntimeError("boom")])
async def test_booking_confirm_unexpected_assign_error_releases_both_locks(exc) -> None:
    ctx = make_real_ctx(db=MagicMock())
    inter, lock, rel = await _run_booking(
        ctx, 40040, ensure=AsyncMock(return_value="dj-me"), assign=AsyncMock(side_effect=exc)
    )
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 1, expected_holder=40040)
    assert inter.response.send_message.call_args[0][0] == texts.GENERIC_ERROR
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_cancelled_releases_pg_lock_and_propagates() -> None:
    import asyncio

    rel, lock, inter, raised = await _drive_booking(
        40041, assign=AsyncMock(side_effect=asyncio.CancelledError())
    )
    assert isinstance(raised, asyncio.CancelledError)
    rel.assert_awaited_once()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_booking_confirm_assign_false_log_names_user_not_none_holder() -> None:
    from bot.ui.views import BookingConfirmView

    ctx = make_real_ctx(db=MagicMock())
    log = MagicMock()
    log.post_error = AsyncMock()
    ctx.log_service = log
    lock = ctx.get_user_lock(40042)
    await lock.acquire()
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = AsyncMock(return_value=False)
    ctx.notion_client.list_events = AsyncMock(return_value=[_gap_event()])
    order: list[str] = []

    async def holder(*a):
        order.append("holder")
        return 40042

    async def release(*a, **k):
        assert k == {"expected_holder": 40042}
        order.append("release")

    view = BookingConfirmView(
        ctx=ctx, event=_gap_event(), slot_index=1, slot_unix=1, lock=lock, user_id=40042
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.db.bookings.release_slot_lock", new=release), \
         patch("bot.db.bookings.get_slot_lock_holder", new=holder), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-me")):
        await view._handle_confirm(inter)
    trigger = log.post_error.call_args.kwargs["trigger"]
    assert "40042" in trigger
    assert "locked by DJ None" not in trigger
    assert order == ["holder", "release"]


@pytest.mark.asyncio
async def test_cancel_confirm_unexpected_clear_error_still_releases_both_locks() -> None:
    clear = AsyncMock(side_effect=RuntimeError("boom"))
    ctx, event, inter, lock, rel = await _run_cancel(
        40043, ensure=AsyncMock(return_value="dj-me"), clear=clear
    )
    rel.assert_awaited_once_with(ctx.db, "evt-gap", 2, expected_holder=40043)
    assert "cancelled" in inter.response.send_message.call_args[0][0]
    assert not lock.locked()


# ---------------------------------------------------------------------------
# Review fix round 2: release the Postgres lock exactly once, keep it once booked
# ---------------------------------------------------------------------------


async def _drive_booking(user_id, *, assign, send=None, present=None):
    """Run _handle_confirm; return (release mock, user lock, inter, raised exception)."""
    import contextlib

    from bot.ui.views import BookingConfirmView

    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(user_id)
    await lock.acquire()
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = assign
    ctx.notion_client.list_events = AsyncMock(return_value=[_gap_event()])
    ctx.log_service = None
    ctx.lineup_service = None
    view = BookingConfirmView(
        ctx=ctx, event=_gap_event(), slot_index=1, slot_unix=1, lock=lock, user_id=user_id
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = send or AsyncMock()
    rel = AsyncMock()
    raised = None
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True))
        )
        stack.enter_context(patch("bot.db.bookings.release_slot_lock", new=rel))
        stack.enter_context(
            patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-me"))
        )
        if present is not None:
            stack.enter_context(patch.object(view, "_present_slot_taken", new=present))
        try:
            await view._handle_confirm(inter)
        except BaseException as exc:  # noqa: BLE001 - tests inspect it
            raised = exc
    return rel, lock, inter, raised


@pytest.mark.asyncio
async def test_booking_abort_send_failure_releases_pg_lock_once() -> None:
    rel, lock, inter, raised = await _drive_booking(
        40050,
        assign=AsyncMock(side_effect=RuntimeError("x")),
        send=AsyncMock(side_effect=RuntimeError("HTTP 500")),
    )
    assert rel.await_count == 1
    assert inter.response.send_message.await_count == 1  # no second response
    assert not lock.locked()
    assert isinstance(raised, RuntimeError)


@pytest.mark.asyncio
async def test_booking_present_slot_taken_failure_releases_pg_lock_once() -> None:
    rel, lock, inter, raised = await _drive_booking(
        40052,
        assign=AsyncMock(return_value=False),
        present=AsyncMock(side_effect=RuntimeError("refresh failed")),
    )
    assert rel.await_count == 1
    assert not lock.locked()
    assert inter.response.send_message.await_count == 1  # single error response


@pytest.mark.asyncio
async def test_booking_cancel_after_release_releases_pg_lock_once() -> None:
    import asyncio

    rel, lock, inter, raised = await _drive_booking(
        40053,
        assign=AsyncMock(return_value=False),
        present=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    assert rel.await_count == 1
    assert not lock.locked()
    assert isinstance(raised, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_booking_confirmation_send_failure_keeps_pg_lock_and_no_error_message() -> None:
    send = AsyncMock(side_effect=RuntimeError("HTTP 500"))
    rel, lock, inter, raised = await _drive_booking(
        40054, assign=AsyncMock(return_value=True), send=send
    )
    rel.assert_not_awaited()
    assert send.await_count == 1  # only the confirmation attempt, no failure message
    assert not lock.locked()
    assert raised is None


@pytest.mark.asyncio
async def test_booking_cancel_after_booked_keeps_pg_lock_and_propagates() -> None:
    import asyncio

    send = AsyncMock(side_effect=asyncio.CancelledError())
    rel, lock, inter, raised = await _drive_booking(
        40055, assign=AsyncMock(return_value=True), send=send
    )
    rel.assert_not_awaited()
    assert not lock.locked()
    assert isinstance(raised, asyncio.CancelledError)


# ---------------------------------------------------------------------------
# Task 3: prepare_booking + LineupPostView
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _dj_exists_by_default():
    """LineupPostView checks onboarding; tests default to an onboarded DJ."""
    with patch("bot.ui.views.get_dj_by_discord_id", new=AsyncMock(return_value={"id": 1})):
        yield


def _ctx_with_events(events: list[EventInfo] | Exception):
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    if isinstance(events, Exception):
        ctx.notion_client.list_events = AsyncMock(side_effect=events)
    else:
        ctx.notion_client.list_events = AsyncMock(return_value=events)
    ctx.log_service = MagicMock()
    ctx.log_service.post_error = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_prepare_booking_open_events_returns_text_and_view() -> None:
    from bot.ui import texts
    from bot.ui.views import EventSelectionView, prepare_booking

    event = _make_event(slots=6, djs_booked=2)
    ctx = _ctx_with_events([event])
    start = await prepare_booking(ctx, 1)
    assert start.error is None
    assert isinstance(start.view, EventSelectionView)
    assert start.content == "\n".join([texts.PICK_EVENT, texts.event_line(1, event, 4)])
    assert ctx.get_user_lock(1).locked()


@pytest.mark.asyncio
async def test_prepare_booking_flow_open() -> None:
    from bot.ui import texts
    from bot.ui.views import prepare_booking

    ctx = _ctx_with_events([_make_event()])
    await ctx.get_user_lock(2).acquire()
    start = await prepare_booking(ctx, 2)
    assert start.error == texts.FLOW_OPEN
    assert start.view is None
    assert ctx.get_user_lock(2).locked()  # the open flow's lock is untouched


@pytest.mark.asyncio
async def test_prepare_booking_all_booked_releases_lock() -> None:
    from bot.ui import texts
    from bot.ui.views import prepare_booking

    ctx = _ctx_with_events([_make_event(slots=3, djs_booked=3)])
    start = await prepare_booking(ctx, 3)
    assert start.error == texts.ALL_BOOKED
    assert not ctx.get_user_lock(3).locked()


@pytest.mark.asyncio
async def test_prepare_booking_no_events_releases_lock() -> None:
    from bot.ui import texts
    from bot.ui.views import prepare_booking

    ctx = _ctx_with_events([])
    start = await prepare_booking(ctx, 4)
    assert start.error == texts.NO_EVENTS
    assert not ctx.get_user_lock(4).locked()


@pytest.mark.asyncio
async def test_prepare_booking_notion_failure_releases_lock() -> None:
    from bot.ui import texts
    from bot.ui.views import prepare_booking

    ctx = _ctx_with_events(Exception("down"))
    start = await prepare_booking(ctx, 5)
    assert start.error == texts.GENERIC_ERROR
    assert not ctx.get_user_lock(5).locked()


def _post_inter(user_id: int = 77, name: str = "Alice") -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.id = user_id
    inter.author.name = name
    inter.author.display_name = name
    inter.author.send = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_lineup_post_view_is_persistent() -> None:
    from bot.ui import texts
    from bot.ui.views import LineupPostView

    view = LineupPostView(make_real_ctx(db=MagicMock()))
    assert view.timeout is None
    assert view.is_persistent()
    assert [c.custom_id for c in view.children] == ["lineup:book_slot"]
    assert view.children[0].label == texts.BTN_BOOK_POST


@pytest.mark.asyncio
async def test_post_button_sends_dm_and_private_reply() -> None:
    from bot.ui import texts
    from bot.ui.views import EventSelectionView, LineupPostView

    ctx = _ctx_with_events([_make_event()])
    view = LineupPostView(ctx)
    inter = _post_inter()
    await view.book_slot.callback(inter)

    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    inter.author.send.assert_awaited_once()
    assert isinstance(inter.author.send.call_args.kwargs["view"], EventSelectionView)
    assert texts.PICK_EVENT in inter.author.send.call_args.args[0]
    inter.followup.send.assert_awaited_once_with(texts.POST_DM_SENT, ephemeral=True)
    assert ctx.get_user_lock(77).locked()


@pytest.mark.asyncio
async def test_post_button_dm_forbidden_releases_lock_and_logs() -> None:
    from bot.ui import texts
    from bot.ui.embeds import ErrorCode
    from bot.ui.views import LineupPostView

    ctx = _ctx_with_events([_make_event()])
    inter = _post_inter()
    inter.author.send = AsyncMock(
        side_effect=disnake.Forbidden(MagicMock(status=403, reason="x"), "no")
    )
    await LineupPostView(ctx).book_slot.callback(inter)

    assert not ctx.get_user_lock(77).locked()
    inter.followup.send.assert_awaited_once_with(texts.POST_DM_BLOCKED, ephemeral=True)
    ctx.log_service.post_error.assert_awaited_once_with(
        ErrorCode.ERR_DM_FAILED,
        trigger="Alice pressed Book a slot but their DMs are closed.",
        action_taken="They were told privately to allow DMs.",
        manual_action=(
            "Tell Alice yourself. They need to allow DMs from server members "
            "(Discord Settings, Privacy & Safety)."
        ),
    )


@pytest.mark.asyncio
async def test_post_button_other_dm_error_releases_lock() -> None:
    from bot.ui import texts
    from bot.ui.views import LineupPostView

    ctx = _ctx_with_events([_make_event()])
    inter = _post_inter()
    inter.author.send = AsyncMock(
        side_effect=disnake.HTTPException(MagicMock(status=500, reason="x"), "boom")
    )
    await LineupPostView(ctx).book_slot.callback(inter)

    assert not ctx.get_user_lock(77).locked()
    inter.followup.send.assert_awaited_once_with(texts.GENERIC_ERROR, ephemeral=True)


@pytest.mark.asyncio
async def test_post_button_flow_open_no_dm() -> None:
    from bot.ui import texts
    from bot.ui.views import LineupPostView

    ctx = _ctx_with_events([_make_event()])
    await ctx.get_user_lock(77).acquire()
    inter = _post_inter()
    await LineupPostView(ctx).book_slot.callback(inter)

    inter.author.send.assert_not_awaited()
    inter.followup.send.assert_awaited_once_with(texts.FLOW_OPEN, ephemeral=True)
    assert ctx.get_user_lock(77).locked()


@pytest.mark.asyncio
async def test_post_button_no_open_events_replies_privately() -> None:
    from bot.ui import texts
    from bot.ui.views import LineupPostView

    ctx = _ctx_with_events([])
    inter = _post_inter()
    await LineupPostView(ctx).book_slot.callback(inter)

    inter.author.send.assert_not_awaited()
    inter.followup.send.assert_awaited_once_with(texts.NO_EVENTS, ephemeral=True)
    assert not ctx.get_user_lock(77).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(("slot_count", "note"), [(25, True), (24, False), (3, False)])
async def test_picker_note_only_when_slots_hidden(slot_count: int, note: bool) -> None:
    from bot.ui.views import EventSelectionView, OutreachView

    expected = f"{texts.PICK_SLOT}\n{texts.PICK_SLOT_TRUNCATED}" if note else texts.PICK_SLOT
    event = make_event(page_id="evt-n", slot_count=slot_count)

    ctx = make_real_ctx(db=None)
    lock = ctx.get_user_lock(40060)
    await lock.acquire()
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.response.send_message = AsyncMock()
    await EventSelectionView(ctx, [event], lock, 40060)._handle_event(inter, event)
    assert inter.response.send_message.call_args.kwargs["content"] == expected

    ctx2 = make_real_ctx(db=None)
    ctx2.notion_client = MagicMock()
    ctx2.notion_client.list_events = AsyncMock(return_value=[event])
    inter2 = MagicMock(spec=disnake.MessageInteraction)
    inter2.author.id = 40061
    inter2.response.send_message = AsyncMock()
    await OutreachView(ctx=ctx2, event=event, discord_user_id=40061).accept.callback(inter2)
    assert inter2.response.send_message.call_args.kwargs["content"] == expected


@pytest.mark.asyncio
async def test_post_button_not_onboarded_gets_no_profile_dm_and_no_lock() -> None:
    from bot.ui import texts
    from bot.ui.views import LineupPostView

    ctx = _ctx_with_events([_make_event()])
    inter = _post_inter()
    with patch("bot.ui.views.get_dj_by_discord_id", new=AsyncMock(return_value=None)):
        await LineupPostView(ctx).book_slot.callback(inter)

    inter.author.send.assert_awaited_once_with(texts.NO_PROFILE)
    inter.followup.send.assert_awaited_once_with(texts.POST_DM_SENT, ephemeral=True)
    ctx.notion_client.list_events.assert_not_awaited()
    assert not ctx.get_user_lock(77).locked()


@pytest.mark.asyncio
async def test_post_button_not_onboarded_dm_blocked_logs_and_no_lock() -> None:
    from bot.ui import texts
    from bot.ui.embeds import ErrorCode
    from bot.ui.views import LineupPostView

    ctx = _ctx_with_events([_make_event()])
    inter = _post_inter()
    inter.author.send = AsyncMock(
        side_effect=disnake.Forbidden(MagicMock(status=403, reason="x"), "no")
    )
    with patch("bot.ui.views.get_dj_by_discord_id", new=AsyncMock(return_value=None)):
        await LineupPostView(ctx).book_slot.callback(inter)

    inter.followup.send.assert_awaited_once_with(texts.POST_DM_BLOCKED, ephemeral=True)
    assert ctx.log_service.post_error.await_args.args[0] is ErrorCode.ERR_DM_FAILED
    assert not ctx.get_user_lock(77).locked()


def test_open_slot_indices_uses_lowest_page_id_row() -> None:
    from bot.ui.views import _open_slot_indices

    held_last = make_event(slot_count=2, slots=[
        make_slot(1, dj_page_id="d", dj_name="X", page_id="p2"), make_slot(1, page_id="p1")])
    held_first = make_event(slot_count=2, slots=[
        make_slot(1, page_id="p2"), make_slot(1, dj_page_id="d", dj_name="X", page_id="p1")])
    assert _open_slot_indices(held_last) == [0, 1]
    assert _open_slot_indices(held_first) == [1]


@pytest.mark.asyncio
async def test_cancel_confirm_success_notifies_lineup() -> None:
    lineup = MagicMock()
    await _run_cancel(
        40040, ensure=AsyncMock(return_value="dj-me"), clear=AsyncMock(return_value=True),
        lineup=lineup,
    )
    lineup.notify_changed.assert_called_once_with()


@pytest.mark.asyncio
async def test_cancel_confirm_success_logs_one_cancelled_entry() -> None:
    from bot.ui.views import CancelConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.clear_slot = AsyncMock(return_value=True)
    log_service = MagicMock()
    log_service.log_cancelled = AsyncMock()
    ctx.log_service = log_service
    ctx.lineup_service = None
    lock = ctx.get_user_lock(30099)
    await lock.acquire()

    event = _make_event(event_id="evt-cancel-log", djs_booked=1)
    view = CancelConfirmView(
        ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=30099
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.display_name = "Nova"
    inter.response.send_message = AsyncMock()

    with patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="page")), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()):
        await view._handle_confirm(inter)

    log_service.log_cancelled.assert_awaited_once_with("Nova", event, 0)


@pytest.mark.asyncio
async def test_booking_confirm_success_without_log_service() -> None:
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20008)
    await lock.acquire()
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = AsyncMock(return_value=True)
    ctx.notion_client.list_events = AsyncMock(return_value=[])
    ctx.log_service = None
    view = _make_booking_confirm_view(ctx, lock, user_id=20008, slot_index=2)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.response.send_message = AsyncMock()
    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-page")):
        await view._handle_confirm(inter)
    inter.response.send_message.assert_awaited_once()
    assert not lock.locked()


# --- New DJ log entry and fully booked hooks (group 4, task 3) ----------------


@pytest.mark.asyncio
async def test_booking_by_last_open_slot_logs_fully_booked(monkeypatch) -> None:
    from tests.services.test_fully_booked import FakeStore

    store = FakeStore()
    store.patch(monkeypatch)
    ctx = make_real_ctx(db=MagicMock())
    lock = ctx.get_user_lock(20008)
    await lock.acquire()
    full = make_event(
        page_id="evt-full",
        slot_count=2,
        slots=[
            make_slot(1, dj_page_id="d1", dj_name="A"),
            make_slot(2, dj_page_id="d2", dj_name="B"),
        ],
    )
    ctx.notion_client = MagicMock()
    ctx.notion_client.assign_slot = AsyncMock(return_value=True)
    ctx.notion_client.list_events = AsyncMock(return_value=[full])
    ctx.log_service = MagicMock()
    ctx.log_service.log_booked = AsyncMock()
    ctx.log_service.log_fully_booked = AsyncMock()
    view = _make_booking_confirm_view(ctx, lock, user_id=20008, slot_index=2)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.display_name = "Nova"
    inter.response.send_message = AsyncMock()

    with patch("bot.db.bookings.acquire_slot_lock", new=AsyncMock(return_value=True)), \
         patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="dj-page")):
        await view._handle_confirm(inter)

    ctx.log_service.log_fully_booked.assert_awaited_once_with(full)


@pytest.mark.asyncio
async def test_cancellation_reopens_event_and_clears_fully_booked(monkeypatch) -> None:
    from bot.ui.views import CancelConfirmView
    from tests.services.test_fully_booked import FakeStore

    store = FakeStore()
    store.rows.add("evt-open")
    store.patch(monkeypatch)
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.clear_slot = AsyncMock(return_value=True)
    opened = make_event(
        page_id="evt-open", slot_count=2, slots=[make_slot(1, dj_page_id="d1"), make_slot(2)]
    )
    ctx.notion_client.list_events = AsyncMock(return_value=[opened])
    ctx.log_service = MagicMock()
    ctx.log_service.log_cancelled = AsyncMock()
    ctx.log_service.log_fully_booked = AsyncMock()
    ctx.lineup_service = None
    lock = ctx.get_user_lock(30098)
    await lock.acquire()
    event = _make_event(event_id="evt-cancel-fb", djs_booked=1)
    view = CancelConfirmView(
        ctx, event=event, slot_index=0, slot_unix=1751414400, lock=lock, user_id=30098
    )
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author = MagicMock()
    inter.author.display_name = "Nova"
    inter.response.send_message = AsyncMock()

    with patch("bot.services.dj_pages.ensure_dj_page", new=AsyncMock(return_value="page")), \
         patch("bot.db.bookings.release_slot_lock", new=AsyncMock()):
        await view._handle_confirm(inter)

    assert store.rows == set()
    ctx.log_service.log_fully_booked.assert_not_awaited()


@pytest.mark.asyncio
async def test_booking_confirm_rechecks_fully_booked_after_lock_release() -> None:
    ctx = make_real_ctx(db=MagicMock())
    seen = []

    async def recheck(*args, **kwargs):
        seen.append(ctx.get_user_lock(40020).locked())

    with patch("bot.services.fully_booked.recheck_fully_booked", new=recheck):
        _, lock, _ = await _run_booking(
            ctx, 40020, ensure=AsyncMock(return_value="dj-me")
        )
    assert seen == [False]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancel_confirm_rechecks_fully_booked_after_lock_release() -> None:
    seen = []
    holder = {}

    async def recheck(*args, **kwargs):
        seen.append(holder["lock"].locked())

    import bot.ui.views as views_mod

    orig = views_mod.CancelConfirmView._handle_confirm

    async def spy(self, inter):
        holder["lock"] = self._lock
        await orig(self, inter)

    with patch("bot.services.fully_booked.recheck_fully_booked", new=recheck), \
         patch.object(views_mod.CancelConfirmView, "_handle_confirm", spy):
        _, _, _, lock, _ = await _run_cancel(
            40021, ensure=AsyncMock(return_value="dj-me"), clear=AsyncMock(return_value=True)
        )
    assert seen == [False]
    assert not lock.locked()


def _stored_invites_role(role_id: str):
    return patch("bot.services.roles.get_value", new=AsyncMock(return_value=role_id))


def _fallback_inter_with_stored_role():
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = 77777
    inter.response.send_message = AsyncMock()
    old = MagicMock(spec=disnake.Role)
    old.name = "Active Booking"
    old.id = 1
    stored = MagicMock(spec=disnake.Role)
    stored.name = "Invites"
    stored.id = 5
    member = MagicMock(spec=disnake.Member)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    guild = MagicMock(spec=disnake.Guild)
    guild.id = 42
    guild.roles = [old, stored]
    guild.get_role = lambda rid: stored if rid == 5 else None
    guild.get_member.return_value = member
    inter.client.guilds = [guild]
    return inter, member, stored


@pytest.mark.asyncio
async def test_fallback_go_active_uses_stored_invites_role() -> None:
    from bot.ui.views import FallbackMenuView

    view = FallbackMenuView(make_real_ctx(db=MagicMock()))
    inter, member, stored = _fallback_inter_with_stored_role()
    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=True)):
        with _stored_invites_role("5"):
            await view.go_active.callback(inter)
    member.add_roles.assert_awaited_once()
    assert member.add_roles.call_args[0][0] is stored


@pytest.mark.asyncio
async def test_fallback_go_inactive_uses_stored_invites_role() -> None:
    from bot.ui.views import FallbackMenuView

    view = FallbackMenuView(make_real_ctx(db=MagicMock()))
    inter, member, stored = _fallback_inter_with_stored_role()
    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=True)):
        with _stored_invites_role("5"):
            await view.go_inactive.callback(inter)
    member.remove_roles.assert_awaited_once()
    assert member.remove_roles.call_args[0][0] is stored


@pytest.mark.asyncio
async def test_fallback_go_active_deleted_stored_role_is_handled() -> None:
    from bot.ui.views import FallbackMenuView

    view = FallbackMenuView(make_real_ctx(db=MagicMock()))
    inter, member, _ = _fallback_inter_with_stored_role()
    with patch("bot.ui.views.update_active_booking", new=AsyncMock(return_value=True)):
        with _stored_invites_role("999"):
            await view.go_active.callback(inter)
    member.add_roles.assert_not_awaited()
    inter.response.send_message.assert_awaited_once()
