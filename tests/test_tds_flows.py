"""The booking flows end to end in the tds layout: views and admin service on a fake plan.

The client tests (test_tds_layout.py) cover what reaches Notion; these cover the wiring above
it: which name is written, what is recorded, and that a refused write gives the slot back.
"""
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import disnake
import httpx
import pytest

from bot import schedule, slots
from bot.notion import props
from tests.conftest import make_real_ctx
from tests.test_tds_layout import PLANNING, FakePlan, make_client

COL = "22:00 - 23:00"  # slot index 1
AB = "bot.services.admin_booking."


@pytest.fixture(autouse=True)
def _tds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(schedule, "_current", schedule.Schedule(
        tz=ZoneInfo("Europe/Berlin"), start=dt.time(21, 0), slot_minutes=60
    ))
    slots.configure(slots.TDS, frozenset(PLANNING), frozenset({"CANCLED", "CANCLED (moved)"}))
    yield
    slots.configure(slots.CLASSIC)


class QueryablePlan(FakePlan):
    """The fake plan also answers the events query with its one page."""

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": [self.page], "has_more": False})
        return await super().handler(request)


def cell(fake: FakePlan) -> str:
    return props.plain_text(fake.page["properties"][COL])


def _inter(user_id: int = 42) -> MagicMock:
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author.id = user_id
    inter.author.name = "dj"
    inter.response.send_message = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    return inter


async def _booking(fake: FakePlan):
    from bot.ui.views import BookingConfirmView

    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = make_client(fake)
    lock = ctx.get_user_lock(42)
    await lock.acquire()
    view = BookingConfirmView(ctx=ctx, event=dict(fake.page), slot_index=1, slot_unix=0,
                              lock=lock, user_id=42)
    return ctx, lock, view


def _db_patches(**overrides):
    values = {
        "bot.db.bookings.acquire_slot_lock": AsyncMock(return_value=True),
        "bot.db.bookings.release_slot_lock": AsyncMock(return_value=True),
        "bot.db.bookings.set_plan_text": AsyncMock(),
        "bot.db.djs.get_notion_dj_page_id": AsyncMock(return_value=None),
        "bot.ui.views.get_dj_by_discord_id": AsyncMock(return_value={"dj_name": "DJ Rayla"}),
    }
    values.update(overrides)
    return values


def _apply(patches: dict):
    from contextlib import ExitStack

    stack = ExitStack()
    for target, mock in patches.items():
        stack.enter_context(patch(target, new=mock))
    return stack


@pytest.mark.asyncio
async def test_dm_booking_writes_the_name_and_records_it() -> None:
    fake = FakePlan()
    _, lock, view = await _booking(fake)
    mocks = _db_patches()
    with _apply(mocks):
        await view._handle_confirm(_inter())
    assert cell(fake) == "DJ Rayla"
    mocks["bot.db.bookings.set_plan_text"].assert_awaited_once()
    assert mocks["bot.db.bookings.set_plan_text"].call_args.args[1:] == ("evt", 1, 42, "DJ Rayla")
    mocks["bot.db.bookings.release_slot_lock"].assert_not_awaited()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_dm_booking_over_a_hand_entry_gives_the_slot_back() -> None:
    fake = FakePlan({COL: "Alex (note)"})
    _, lock, view = await _booking(fake)
    mocks = _db_patches()
    inter = _inter()
    with _apply(mocks):
        await view._handle_confirm(inter)
    assert cell(fake) == "Alex (note)"
    mocks["bot.db.bookings.release_slot_lock"].assert_awaited_once()
    mocks["bot.db.bookings.set_plan_text"].assert_not_awaited()
    text = inter.followup.send.call_args or inter.response.send_message.call_args
    assert "just filled" in text.args[0] and "Alex" not in text.args[0]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_dm_booking_after_the_event_was_called_off_gives_the_slot_back() -> None:
    fake = FakePlan()
    _, _, view = await _booking(fake)  # the menu was opened while it was in planning
    fake.page["properties"]["Status"]["status"]["name"] = "CANCLED"
    mocks = _db_patches()
    inter = _inter()
    with _apply(mocks):
        await view._handle_confirm(inter)
    assert cell(fake) == ""
    mocks["bot.db.bookings.release_slot_lock"].assert_awaited_once()
    text = inter.followup.send.call_args or inter.response.send_message.call_args
    assert "isn't open for booking" in text.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("holds_another", [False, True])
async def test_dm_cancel_clears_the_recorded_text(holds_another: bool) -> None:
    """Every slot has its own column: cleared even while the DJ keeps another slot."""
    from bot.ui.views import CancelConfirmView

    fake = FakePlan({COL: "Old Name"})
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = make_client(fake)
    lock = ctx.get_user_lock(42)
    await lock.acquire()
    view = CancelConfirmView(ctx=ctx, event=dict(fake.page), slot_index=1, slot_unix=0,
                             lock=lock, user_id=42)
    mocks = {
        "bot.db.bookings.release_slot_lock": AsyncMock(return_value=True),
        "bot.db.bookings.holds_slot_in_event": AsyncMock(return_value=holds_another),
        "bot.db.bookings.get_plan_text": AsyncMock(return_value="Old Name"),
        "bot.db.djs.get_notion_dj_page_id": AsyncMock(return_value=None),
        "bot.ui.views.get_dj_by_discord_id": AsyncMock(return_value={"dj_name": "New Name"}),
    }
    with _apply(mocks):
        await view._handle_confirm(_inter())
    assert cell(fake) == ""


def _service(fake: FakePlan):
    from bot.services.admin_booking import AdminBookingService

    return AdminBookingService(bot=MagicMock(), db=MagicMock(), notion_client=make_client(fake),
                               log_service=None)


def _names(by_id: dict[int, str]):
    def row(pool, uid):
        return {"dj_name": by_id[uid]} if uid in by_id else None

    return AsyncMock(side_effect=row)


@pytest.mark.asyncio
async def test_admin_book_overwrites_exactly_the_displaced_djs_recorded_text() -> None:
    fake = QueryablePlan({COL: "Old DJ"})
    svc = _service(fake)
    set_text = AsyncMock()
    with patch(AB + "get_slot_lock_holder", new=AsyncMock(return_value=999)), \
         patch(AB + "get_plan_text", new=AsyncMock(return_value="Old DJ")), \
         patch(AB + "get_dj_by_discord_id",
               new=_names({42: "New DJ", 999: "Old DJ (renamed)"})), \
         patch(AB + "get_notion_dj_page_id", new=AsyncMock(return_value=None)), \
         patch(AB + "admin_force_book_slot", new=AsyncMock()), \
         patch(AB + "set_plan_text", new=set_text):
        ok, msg = await svc.book_slot_override("Admin", 42, "New", "2026-10-24", 2)
    assert ok, msg
    assert cell(fake) == "New DJ"
    assert set_text.call_args.args[1:] == ("evt", 1, 42, "New DJ")


@pytest.mark.asyncio
async def test_admin_book_never_overwrites_a_hand_entry() -> None:
    fake = QueryablePlan({COL: "Echo Fox"})
    svc = _service(fake)
    release = AsyncMock(return_value=True)
    with patch(AB + "get_slot_lock_holder", new=AsyncMock(return_value=None)), \
         patch(AB + "get_dj_by_discord_id", new=_names({42: "New DJ"})), \
         patch(AB + "get_notion_dj_page_id", new=AsyncMock(return_value=None)), \
         patch(AB + "admin_force_book_slot", new=AsyncMock()), \
         patch(AB + "release_slot_lock", new=release):
        ok, msg = await svc.book_slot_override("Admin", 42, "New", "2026-10-24", 2)
    assert not ok and "typed in by hand" in msg
    assert cell(fake) == "Echo Fox"
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_book_refuses_the_open_decks_column() -> None:
    fake = QueryablePlan()
    svc = _service(fake)
    force = AsyncMock()
    with patch(AB + "admin_force_book_slot", new=force):
        ok, msg = await svc.book_slot_override("Admin", 42, "New", "2026-10-24", 7)
    assert not ok and "slots 1 to 6" in msg
    force.assert_not_awaited()
    assert fake.patches == []


@pytest.mark.asyncio
async def test_admin_cancel_clears_the_recorded_text() -> None:
    fake = QueryablePlan({COL: "Old Name"})
    svc = _service(fake)
    with patch(AB + "get_slot_lock_holder", new=AsyncMock(return_value=42)), \
         patch(AB + "get_plan_text", new=AsyncMock(return_value="Old Name")), \
         patch(AB + "get_dj_by_discord_id", new=_names({42: "New Name"})), \
         patch(AB + "release_slot_lock", new=AsyncMock(return_value=True)), \
         patch(AB + "get_notion_dj_page_id", new=AsyncMock(return_value=None)):
        ok, msg = await svc.cancel_slot_override("Admin", 42, "DJ", "2026-10-24", 2)
    assert ok, msg
    assert cell(fake) == ""


def test_admin_finds_the_live_event_next_to_a_called_off_one() -> None:
    from tests.test_tds_layout import plan_event

    svc = _service(FakePlan())
    moved, new = plan_event(event_id="a", status="CANCLED (moved)"), plan_event(event_id="b")
    event, problem = svc._find_event([moved, new], "2026-10-24")
    assert event is new and problem == ""


@pytest.mark.asyncio
@pytest.mark.real_slots  # the real annotate(), not the conftest stand-in
async def test_a_missing_plan_column_is_not_offered() -> None:
    from tests.test_tds_layout import plan_event

    schedule._current = schedule.Schedule(tz=ZoneInfo("Europe/Berlin"), start=dt.time(20, 0),
                                          slot_minutes=60)  # there is no 20:00 - 21:00 column
    event = plan_event()
    with patch("bot.slots.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.slots.get_booked_slots", new=AsyncMock(return_value={})):
        await slots.annotate(MagicMock(), 1, [event])
    st = slots.state(event)
    assert 0 in st.unavailable and 0 not in st.open_indices


@pytest.mark.asyncio
async def test_no_reminders_for_a_night_called_off() -> None:
    from bot.services.reminders import RemindersService
    from tests.test_tds_layout import plan_event

    called_off, live = plan_event(event_id="a", status="CANCLED"), plan_event(event_id="b")
    notion = MagicMock(poll_events=AsyncMock(return_value=[called_off, live]))
    svc = RemindersService(bot=MagicMock(), db=MagicMock(), notion_client=notion,
                           log_service=MagicMock(post_error=AsyncMock()))
    booked = AsyncMock(return_value=[])
    with patch.object(RemindersService, "_is_in_reminder_window", return_value=True), \
         patch("bot.services.reminders.get_booked_djs_for_event", new=booked):
        await svc.send_reminders(guild_id=1, ctx=MagicMock())
    assert [c.args[1] for c in booked.await_args_list] == ["b"]


@pytest.mark.asyncio
@pytest.mark.real_slots  # the real annotate(): the names come from the plan
@pytest.mark.parametrize("dj_name,contacted", [("dj rayla", False), ("Pulse", True)])
async def test_outreach_skips_a_dj_already_typed_into_the_plan(dj_name, contacted) -> None:
    """The DJ typed in by hand is skipped; another DJ in the same setup is invited."""
    from bot.services.outreach import OutreachService
    from tests.test_tds_layout import plan_event

    event = plan_event({"23:00 - 24:00": "DJ Rayla"})
    bot = MagicMock()
    user = MagicMock(send=AsyncMock())
    bot.get_user = MagicMock(return_value=user)
    svc = OutreachService(bot=bot, db=MagicMock(), notion_client=MagicMock(),
                          log_service=MagicMock(post_error=AsyncMock(),
                                                post_dm_transcript=AsyncMock()))
    dj = {"discord_user_id": 42, "dj_name": dj_name, "last_contacted_at": None}
    ctx = MagicMock()
    with patch("bot.slots.get_value", new=AsyncMock(return_value=None)), \
         patch("bot.slots.get_booked_slots", new=AsyncMock(return_value={})), \
         patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[dj])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.is_under_intervention", new=AsyncMock(return_value=False)), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()), \
         patch("bot.services.outreach.update_last_contact_at", new=AsyncMock()), \
         patch("bot.services.outreach.update_dj_last_contacted_at", new=AsyncMock()):
        assert await svc.contact_next_dj(event, guild_id=1, ctx=ctx) is contacted
    assert user.send.await_count == (1 if contacted else 0)


@pytest.mark.asyncio
async def test_the_lineup_leaves_out_a_night_called_off() -> None:
    from bot.services.lineup import LineupService
    from tests.test_tds_layout import plan_event

    class Captured(Exception):
        pass

    shown: list = []

    def capture(events):
        shown.extend(events)
        raise Captured

    called_off, live = plan_event(event_id="a", status="CANCLED (moved)"), plan_event(event_id="b")
    bot = MagicMock()
    bot.guilds = [MagicMock(id=1)]
    svc = LineupService(bot=bot, db=MagicMock(), log_service=MagicMock(post_error=AsyncMock()))
    with patch("bot.services.lineup.get_value", new=AsyncMock(return_value="555")), \
         patch("bot.services.lineup.MissionRosterEmbed", side_effect=capture), \
         pytest.raises(Captured):
        await svc.post_update([called_off, live])
    assert [e["id"] for e in shown] == ["b"]
