"""Tests for ManagementInfoView: the admin-only "Send me more information" button."""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.db.management_lineup import LineupRecord
from bot.notion.models import EventInfo, SlotInfo
from bot.ui import texts
from bot.ui.management_lineup import ManagementInfoView
from tests.conftest import make_real_ctx

ADMIN = 111
M = "bot.ui.management_lineup"


def _slot(n, uid):
    return SlotInfo(page_id=f"s{n}", number=n, dj_page_id=f"p{n}", dj_name=f"DJ{n}", discord_id=uid)


def _event(count=3):
    return EventInfo(
        page_id="ev1", event_id="Rave 1", date=datetime.date(2026, 12, 5), status="Planned",
        genre="Techno", notes=None, slot_count=count, start=datetime.time(20, 0),
        slot_minutes=60, slots=tuple(_slot(n, 100 + n) for n in range(1, count + 1)),
        location="Club",
    )


def _details():
    return {
        100 + n: {
            "discord_user_id": 100 + n, "dj_name": f"DJ{n}", "vrchat_name": f"vrc{n}",
            "stream_url_primary": f"https://s{n}.example/a", "stream_url_backup": None,
            "logo_url": None, "has_logo": n == 1,
        }
        for n in (1, 2, 3)
    }


def _ctx():
    ctx = make_real_ctx(db=MagicMock())
    ctx.config.admin_user_ids = frozenset({ADMIN})
    return ctx


def _inter(author_id=ADMIN):
    inter = MagicMock()
    inter.author.id = author_id
    inter.author.send = AsyncMock()
    inter.message.id = 500
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.bot.get_user = MagicMock(return_value=SimpleNamespace(name="disc", display_name="Disc"))
    inter.bot.fetch_user = AsyncMock()
    return inter


def _patches(record="ok"):
    rec = LineupRecord("ev1", 9, (500,), "h") if record == "ok" else None
    return [
        patch(f"{M}.get_record_by_message", AsyncMock(return_value=rec)),
        patch(f"{M}.db_events.get_event", AsyncMock(return_value=_event())),
        patch(f"{M}.db_djs.get_dj_details", AsyncMock(return_value=_details())),
        patch(
            f"{M}.db_djs.get_logo_image",
            AsyncMock(side_effect=lambda pool, uid: b"\x89PNGdata" if uid == 101 else None),
        ),
    ]


async def _click(view, inter, record="ok"):
    ps = _patches(record)
    for p in ps:
        p.start()
    try:
        await view.send_info.callback(inter)
    finally:
        for p in ps:
            p.stop()
    return ps


def _all_text(inter):
    calls = [*inter.author.send.await_args_list, *inter.response.send_message.await_args_list,
             *inter.followup.send.await_args_list]
    return "\n".join(str(c) for c in calls)


@pytest.mark.asyncio
async def test_view_is_persistent_with_button() -> None:
    view = ManagementInfoView(_ctx())
    assert view.timeout is None
    btn = view.children[0]
    assert btn.custom_id == "mgmt:info"
    assert btn.label == "Send me more information"
    assert btn.style == disnake.ButtonStyle.secondary


@pytest.mark.asyncio
async def test_non_admin_refused_and_no_data_loaded() -> None:
    view = ManagementInfoView(_ctx())
    inter = _inter(author_id=999)
    ps = [
        patch(f"{M}.get_record_by_message", AsyncMock()),
        patch(f"{M}.db_events.get_event", AsyncMock()),
        patch(f"{M}.db_djs.get_dj_details", AsyncMock()),
        patch(f"{M}.db_djs.get_logo_image", AsyncMock()),
    ]
    mocks = [p.start() for p in ps]
    try:
        await view.send_info.callback(inter)
    finally:
        for p in ps:
            p.stop()
    for m in mocks:
        m.assert_not_awaited()
    inter.author.send.assert_not_awaited()
    inter.response.send_message.assert_awaited_once_with(
        texts.MGMT_INFO_ADMINS_ONLY, ephemeral=True
    )
    text = _all_text(inter)
    assert "DJ1" not in text and "example" not in text


@pytest.mark.asyncio
async def test_admin_gets_header_plus_one_dm_per_dj() -> None:
    view = ManagementInfoView(_ctx())
    inter = _inter()
    await _click(view, inter)
    sends = inter.author.send.await_args_list
    assert len(sends) == 4
    assert "Rave 1" in sends[0].args[0]
    assert "file" not in sends[0].kwargs
    assert "DJ1" in sends[1].args[0] and "Discord: Disc" in sends[1].args[0]
    assert isinstance(sends[1].kwargs["file"], disnake.File)
    assert sends[1].kwargs["file"].filename == "logo-slot-1.png"
    assert "file" not in sends[2].kwargs
    inter.followup.send.assert_awaited_once_with(texts.MGMT_INFO_SENT, ephemeral=True)


@pytest.mark.asyncio
async def test_discord_name_falls_back_to_none() -> None:
    view = ManagementInfoView(_ctx())
    inter = _inter()
    inter.bot.get_user = MagicMock(return_value=None)
    inter.bot.fetch_user = AsyncMock(side_effect=disnake.NotFound(MagicMock(), "x"))
    await _click(view, inter)
    assert "Discord: none" in inter.author.send.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_closed_dms_give_private_note() -> None:
    view = ManagementInfoView(_ctx())
    inter = _inter()
    inter.author.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(), "closed"))
    await _click(view, inter)
    inter.followup.send.assert_awaited_once_with(texts.MGMT_INFO_DMS_CLOSED, ephemeral=True)


@pytest.mark.asyncio
async def test_failure_on_third_dj_dm_reports_two_sent() -> None:
    view = ManagementInfoView(_ctx())
    inter = _inter()
    inter.author.send = AsyncMock(
        side_effect=[None, None, None, disnake.HTTPException(MagicMock(), "boom")]
    )
    await _click(view, inter)
    # header, DJ1, DJ2 sent; DJ3 failed: 2 DJs sent
    inter.followup.send.assert_awaited_once_with(texts.mgmt_info_partial(2), ephemeral=True)


@pytest.mark.asyncio
async def test_unknown_message_replies_ephemeral() -> None:
    view = ManagementInfoView(_ctx())
    inter = _inter()
    await _click(view, inter, record=None)
    inter.author.send.assert_not_awaited()
    assert texts.MGMT_INFO_UNTRACKED in _all_text(inter)
    assert "This lineup is no longer tracked" in texts.MGMT_INFO_UNTRACKED


@pytest.mark.asyncio
async def test_maintenance_refuses() -> None:
    ctx = _ctx()
    ctx.maintenance = texts.MAINTENANCE
    view = ManagementInfoView(ctx)
    inter = _inter()
    await _click(view, inter)
    inter.author.send.assert_not_awaited()
    inter.response.send_message.assert_awaited_once_with(texts.MAINTENANCE, ephemeral=True)


@pytest.mark.asyncio
async def test_runtime_added_admin_is_accepted() -> None:
    ctx = _ctx()
    ctx.admins = MagicMock()
    ctx.admins.is_admin = MagicMock(side_effect=lambda uid: uid == 777)
    view = ManagementInfoView(ctx)
    inter = _inter(author_id=777)
    await _click(view, inter)
    assert inter.author.send.await_count == 4
