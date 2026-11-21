"""Tests for bot/ui/schedule_views.py (ApplyChangesView)."""
import datetime
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.services.schedule import PendingChange
from bot.ui import texts
from bot.ui.schedule_views import ApplyChangesView
from tests.notion.factories import make_event


def _changes(n: int) -> list[PendingChange]:
    day = datetime.date(2026, 12, 5)
    return [
        PendingChange(
            make_event(page_id=f"uid-{i}", event_id=f"{day} Rave Night", date=day), "genre",
            "House", "Techno",
        )
        for i in range(n)
    ]


def _view(n: int = 2, admin: int = 7):
    ctx = MagicMock()
    ctx.is_admin = lambda uid: uid == admin
    ctx.maintenance = None
    ctx.schedule_service.apply = AsyncMock(return_value=n)
    return ctx, ApplyChangesView(ctx, 111, admin, _changes(n))


def _inter(uid: int = 7, values=None):
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = uid
    inter.values = values
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.edit_original_message = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_content_lists_each_change() -> None:
    _, view = _view(1)
    assert "Rave Night · 05.12. · \U0001f3b5 House → Techno" in view.content()


@pytest.mark.asyncio
async def test_apply_view_apply_all() -> None:
    ctx, view = _view(2)
    inter = _inter()
    await view.apply_all.callback(inter)
    ctx.schedule_service.apply.assert_awaited_once_with(111, None)
    assert inter.edit_original_message.call_args.kwargs["content"] == texts.apply_done(2)


@pytest.mark.asyncio
async def test_apply_view_keep_all_writes_nothing() -> None:
    ctx, view = _view(2)
    inter = _inter()
    await view.keep_all.callback(inter)
    ctx.schedule_service.apply.assert_not_awaited()
    assert inter.response.edit_message.call_args.kwargs["content"] == texts.APPLY_KEPT


@pytest.mark.asyncio
async def test_apply_view_pick_events_caps_at_25() -> None:
    _, view = _view(30)
    inter = _inter()
    await view.pick_events.callback(inter)
    sent = inter.response.edit_message.call_args.kwargs
    select = next(c for c in view.children if isinstance(c, disnake.ui.StringSelect))
    assert len(select.options) == 25
    assert all(len(o.label) <= 100 for o in select.options)
    assert select.options[0].value == "uid-0:genre"
    assert texts.APPLY_CAPPED in sent["content"]
    assert len(sent["content"]) <= 2000
    buttons = [c for c in view.children if isinstance(c, disnake.ui.Button)]
    assert [b.label for b in buttons] == [texts.APPLY_SELECTED]


@pytest.mark.asyncio
async def test_apply_view_apply_selected_passes_picks() -> None:
    ctx, view = _view(3)
    await view.pick_events.callback(_inter())
    await view._on_select(_inter(values=["uid-1:genre"]))
    inter = _inter()
    await view._on_confirm(inter)
    ctx.schedule_service.apply.assert_awaited_once_with(111, [("uid-1", "genre")])


@pytest.mark.asyncio
async def test_apply_view_apply_selected_needs_a_pick() -> None:
    ctx, view = _view(3)
    inter = _inter()
    await view._on_confirm(inter)
    ctx.schedule_service.apply.assert_not_awaited()
    assert inter.response.send_message.call_args.args[0] == texts.APPLY_NONE_PICKED


@pytest.mark.asyncio
async def test_apply_view_other_user_refused() -> None:
    _, view = _view(2, admin=7)
    inter = _inter(uid=8)
    assert await view.interaction_check(inter) is False
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs["ephemeral"] is True
    assert await view.interaction_check(_inter(uid=7)) is True


@pytest.mark.asyncio
async def test_view_times_out_after_fifteen_minutes() -> None:
    _, view = _view(1)
    assert view.timeout == 900


@pytest.mark.asyncio
async def test_content_stays_within_discord_limit() -> None:
    _, view = _view(200)
    assert len(view.content("intro")) <= 2000
