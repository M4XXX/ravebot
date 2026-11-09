"""Tests for bot.services.dj_admin (repositories and Notion patched)."""
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from bot.services import dj_admin as mod
from bot.services.dj_admin import DjAdminService, check_link, is_guest, parse_discord_id
from tests.conftest import make_real_ctx

ROW = {
    "discord_user_id": -1, "dj_name": "Moonbeat", "vrchat_name": None,
    "stream_url_primary": "https://twitch.tv/moon", "stream_url_backup": None,
    "active_booking": False, "has_logo": False,
}


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(out, format="PNG")
    return out.getvalue()


def _service(**ctx_attrs):
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.log_service = MagicMock(log_dj_list_change=AsyncMock())
    for key, value in ctx_attrs.items():
        setattr(ctx, key, value)
    return DjAdminService(ctx, MagicMock(get_user=MagicMock(return_value=None))), ctx


def _patch(**fns):
    defaults = {
        "dj_name_exists": AsyncMock(return_value=False),
        "get_dj_row": AsyncMock(return_value=None),
        "next_guest_id": AsyncMock(return_value=-1),
        "insert_dj": AsyncMock(),
        "sync_dj": AsyncMock(return_value=True),
    }
    defaults.update(fns)
    return [patch.object(mod, name, new=fn) for name, fn in defaults.items()]


class _Patched:
    def __init__(self, **fns) -> None:
        self.patches = _patch(**fns)
        self.fns = {p.attribute: p.new for p in self.patches}

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self.fns

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def test_helpers() -> None:
    assert is_guest(-3) and not is_guest(123)
    assert parse_discord_id(" 123456789012345678 ") == 123456789012345678
    assert parse_discord_id("12") is None and parse_discord_id("abc") is None
    assert check_link("twitch.tv/x", "link") == ("https://twitch.tv/x", None)
    assert check_link("rtmp://my.server/live", "link") == ("rtmp://my.server/live", None)
    assert "must be a link" in check_link("not a link", "stream link")[1]
    assert check_link("https://", "link")[0] is None


async def test_add_guest_gets_negative_id_and_is_inactive() -> None:
    service, ctx = _service()
    with _Patched() as fns:
        result = await service.add_dj(
            admin_name="Admin", name=" Moon  beat ", link="twitch.tv/moon", logo=_png()
        )
    assert result.ok and "guest DJ Moon beat" in result.message
    kwargs = fns["insert_dj"].call_args.kwargs
    assert kwargs["discord_user_id"] == -1
    assert kwargs["active_booking"] is False
    assert kwargs["stream_url_primary"] == "https://twitch.tv/moon"
    assert kwargs["logo_image"].startswith(b"\x89PNG")
    fns["sync_dj"].assert_awaited_once_with(ctx, -1, mod.GUEST_NAME)
    ctx.log_service.log_dj_list_change.assert_awaited_once_with("Admin", "added", "Moon beat")


async def test_add_discord_dj_is_active_and_keeps_the_id() -> None:
    service, _ = _service()
    with _Patched() as fns:
        result = await service.add_dj(
            admin_name="A", name="Nova", link="https://youtube.com/n",
            discord_id=123456789012345678,
        )
    assert result.ok and "guest" not in result.message
    kwargs = fns["insert_dj"].call_args.kwargs
    assert kwargs["discord_user_id"] == 123456789012345678 and kwargs["active_booking"] is True
    fns["next_guest_id"].assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"name": "  ", "link": "https://a.b"}, "name is required"),
        ({"name": "x" * 81, "link": "https://a.b"}, "too long"),
        ({"name": "A", "link": "two words"}, "must be a link"),
        ({"name": "A", "link": "https://a.b", "alt_link": "1://"}, "backup link"),
        ({"name": "A", "link": "https://a.b", "vrchat": "v" * 81}, "VRChat name is too long"),
        ({"name": "A", "link": "https://a.b", "logo": b"not an image"}, "logo"),
    ],
)
async def test_add_validation_errors(kwargs, needle) -> None:
    service, _ = _service()
    with _Patched() as fns:
        result = await service.add_dj(admin_name="A", **kwargs)
    assert not result.ok and needle in result.message
    fns["insert_dj"].assert_not_awaited()


async def test_add_rejects_taken_name_and_existing_discord_user() -> None:
    service, _ = _service()
    with _Patched(dj_name_exists=AsyncMock(return_value=True)) as fns:
        result = await service.add_dj(admin_name="A", name="Nova", link="https://a.b")
    assert not result.ok and 'already a DJ called "Nova"' in result.message
    fns["insert_dj"].assert_not_awaited()
    with _Patched(get_dj_row=AsyncMock(return_value=ROW)) as fns:
        result = await service.add_dj(
            admin_name="A", name="Nova", link="https://a.b", discord_id=123456789012345678
        )
    assert not result.ok and "already a DJ" in result.message


async def test_add_reports_notion_failure_but_keeps_the_dj() -> None:
    service, _ = _service()
    with _Patched(sync_dj=AsyncMock(return_value=False)):
        result = await service.add_dj(admin_name="A", name="Nova", link="https://a.b")
    assert result.ok and "Notion could not be updated" in result.message


async def test_edit_changes_fields_and_syncs() -> None:
    service, ctx = _service()
    update = AsyncMock(return_value=True)
    with _Patched(get_dj_row=AsyncMock(return_value=ROW), update_dj_fields=update) as fns:
        result = await service.edit_dj(
            admin_name="A", dj_id=-1,
            fields={"name": "Moon", "link": "twitch.tv/m", "alt_link": "", "vrchat": " Mo "},
            logo=_png(),
        )
    assert result.ok
    args = update.call_args
    assert args.args[1] == -1
    kw = args.kwargs
    assert kw["dj_name"] == "Moon" and kw["stream_url_primary"] == "https://twitch.tv/m"
    assert kw["stream_url_backup"] is None and kw["vrchat_name"] == "Mo"
    assert kw["logo_image"].startswith(b"\x89PNG")
    fns["sync_dj"].assert_awaited_once()
    ctx.log_service.log_dj_list_change.assert_awaited_once()


async def test_edit_errors() -> None:
    service, _ = _service()
    with _Patched(get_dj_row=AsyncMock(return_value=None), update_dj_fields=AsyncMock()):
        result = await service.edit_dj(admin_name="A", dj_id=5, fields={"name": "x"})
        assert result.message == mod.UNKNOWN_DJ
    with _Patched(get_dj_row=AsyncMock(return_value=ROW), update_dj_fields=AsyncMock()):
        result = await service.edit_dj(admin_name="A", dj_id=-1, fields={})
        assert result.message == "Nothing to change."
        assert not (await service.edit_dj(admin_name="A", dj_id=-1, fields={"link": ""})).ok
    with _Patched(
        get_dj_row=AsyncMock(return_value=ROW),
        dj_name_exists=AsyncMock(return_value=True),
        update_dj_fields=AsyncMock(),
    ) as fns:
        result = await service.edit_dj(admin_name="A", dj_id=-1, fields={"name": "Taken"})
    assert not result.ok and "already a DJ" in result.message
    fns["update_dj_fields"].assert_not_awaited()


def _event(page_id="p1", name="Rave"):
    from tests.notion.factories import make_event

    return make_event(event_id=name, page_id=page_id)


async def test_delete_cancels_future_bookings_trashes_notion_and_deletes() -> None:
    booking = MagicMock(cancel_slot_override=AsyncMock(return_value=(True, "")))
    service, ctx = _service(admin_booking_service=booking)
    ctx.notion_client.list_events = AsyncMock(return_value=[_event("p1", "Rave One")])
    ctx.notion_client.trash_dj_page = AsyncMock()
    locks = AsyncMock(return_value=[
        {"event_id": "p1", "slot_index": 1}, {"event_id": "old", "slot_index": 0},
    ])
    delete = AsyncMock(return_value=True)
    with patch.object(mod, "get_dj_slot_locks", new=locks), patch.object(
        mod, "get_notion_dj_page_id", new=AsyncMock(return_value="np")
    ), patch.object(mod, "delete_dj", new=delete), patch.object(
        mod, "get_dj_row", new=AsyncMock(return_value=ROW)
    ):
        result = await service.delete_dj(admin_name="A", dj_id=-1)
    assert result.ok and "1 upcoming booking cancelled" in result.message
    booking.cancel_slot_override.assert_awaited_once_with(
        admin_name="A", dj_id=-1, dj_name="Moonbeat", event_name="Rave One", slot_num=2
    )
    ctx.notion_client.trash_dj_page.assert_awaited_once_with("np")
    delete.assert_awaited_once_with(ctx.db, -1)


async def test_delete_stops_when_a_cancel_fails() -> None:
    booking = MagicMock(cancel_slot_override=AsyncMock(return_value=(False, "Notion down")))
    service, ctx = _service(admin_booking_service=booking)
    ctx.notion_client.list_events = AsyncMock(return_value=[_event("p1", "Rave One")])
    delete = AsyncMock()
    with patch.object(
        mod, "get_dj_slot_locks", new=AsyncMock(return_value=[{"event_id": "p1", "slot_index": 0}])
    ), patch.object(mod, "delete_dj", new=delete), patch.object(
        mod, "get_dj_row", new=AsyncMock(return_value=ROW)
    ):
        result = await service.delete_dj(admin_name="A", dj_id=-1)
    assert not result.ok and "not deleted" in result.message
    delete.assert_not_awaited()


async def test_delete_unknown_dj_and_not_ready() -> None:
    service, ctx = _service(admin_booking_service=MagicMock())
    with patch.object(mod, "get_dj_row", new=AsyncMock(return_value=None)):
        assert (await service.delete_dj(admin_name="A", dj_id=9)).message == mod.UNKNOWN_DJ
    ctx.db = None
    assert (await service.delete_dj(admin_name="A", dj_id=9)).message == mod.NOT_READY


async def test_find_by_name_ignores_case_and_spacing() -> None:
    service, _ = _service()
    with patch.object(mod, "get_all_dj_rows", new=AsyncMock(return_value=[ROW])):
        assert await service.find_by_name("  moonBEAT ") == ROW
        assert await service.find_by_name("nobody") is None


async def test_delete_waits_for_a_dj_who_is_booking() -> None:
    booking = MagicMock(cancel_slot_override=AsyncMock())
    service, ctx = _service(admin_booking_service=booking)
    row = {**ROW, "discord_user_id": 123456789012345678}
    await ctx.get_user_lock(123456789012345678).acquire()
    with patch.object(mod, "get_dj_row", new=AsyncMock(return_value=row)), \
         patch.object(mod, "delete_dj", new=AsyncMock()) as delete:
        result = await service.delete_dj(admin_name="A", dj_id=123456789012345678)
    assert not result.ok and "right now" in result.message
    delete.assert_not_awaited()


async def test_delete_db_error_is_reported() -> None:
    booking = MagicMock(cancel_slot_override=AsyncMock())
    service, ctx = _service(admin_booking_service=booking)
    ctx.notion_client.list_events = AsyncMock(return_value=[])
    ctx.notion_client.trash_dj_page = AsyncMock()
    with patch.object(mod, "get_dj_row", new=AsyncMock(return_value=ROW)), \
         patch.object(mod, "get_dj_slot_locks", new=AsyncMock(return_value=[])), \
         patch.object(mod, "get_notion_dj_page_id", new=AsyncMock(return_value=None)), \
         patch.object(mod, "delete_dj", new=AsyncMock(side_effect=OSError("db"))):
        result = await service.delete_dj(admin_name="A", dj_id=-1)
    assert not result.ok and "not deleted" in result.message
