"""NOTION_LAYOUT=tds: an organizers' plan with one text column per hour slot.

A small in-memory plan (httpx.MockTransport) holds one event page with the columns of the
real TDS planning database; the bot writes DJ names into them and never touches a name it
did not write.
"""
import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from bot import schedule, slots
from bot.notion import props
from bot.notion.client import NotionClient, NotionSlotTakenError, _refuse_deletion

# Column names exactly as in the TDS plan (a real organizers' database), including the unused ones
PLAN_COLUMNS = [
    "18:00 - 19:00", "19:00 - 20:00", "21:00 - 22:00", "22:00 - 23:00", "23:00 - 24:00",
    "24:00 - 01:00", "01:00 - 02:00", "02:00 - 03:00", "03:00 - 04:00", "04:00 - 05:00",
    "05:00 - 06:00",
]
PLANNING = ("Planing-stage", "Not started", "Requires Host and Security", "Requires Security",
            "Requires Host")

pytestmark = pytest.mark.real_slots  # the real annotate(), not the conftest stand-in


@pytest.fixture(autouse=True)
def _tds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(schedule, "_current", schedule.Schedule(
        tz=ZoneInfo("Europe/Berlin"), start=dt.time(21, 0), slot_minutes=60
    ))
    slots.configure(slots.TDS, frozenset(PLANNING))
    yield
    slots.configure(slots.CLASSIC)


def _text(value: str) -> dict:
    return {"type": "rich_text", "rich_text": [{"plain_text": value}] if value else []}


def plan_event(cells: dict[str, str] | None = None, status: str = "Planing-stage",
               event_id: str = "evt") -> dict:
    """An event page as Notion returns it from the TDS plan."""
    properties = {
        "Event": {"type": "title", "title": [{"plain_text": "Casual Event"}]},
        "Date": {"type": "date", "date": {"start": "2026-10-24"}},
        "Status": {"type": "status", "status": {"name": status}},
    }
    for column in PLAN_COLUMNS:
        properties[column] = _text((cells or {}).get(column, ""))
    return {"id": event_id, "properties": properties}


class FakePlan:
    def __init__(self, cells: dict[str, str] | None = None) -> None:
        self.page = plan_event(cells)
        self.patches: list[dict] = []
        self.created: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/pages/"):
            return httpx.Response(200, json=self.page)
        if request.method == "GET" and path.startswith("/v1/databases/"):
            return httpx.Response(200, json={"properties": {
                "Event": {"type": "title"}, "Date": {"type": "date"},
                "Status": {"type": "status"},
            }})
        if request.method == "PATCH":
            body = json.loads(request.content)
            self.patches.append(body)
            for column, value in body["properties"].items():
                text = "".join(p["text"]["content"] for p in value["rich_text"])
                self.page["properties"][column] = _text(text)
            return httpx.Response(200, json=self.page)
        if request.method == "POST" and path == "/v1/pages":
            self.created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "new"})
        if request.method == "POST" and path.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={})


def make_client(fake: FakePlan) -> NotionClient:
    log = MagicMock()
    log.post_error = AsyncMock()
    client = NotionClient(api_key="secret_test", database_id="db", log_service=log,
                          layout=slots.TDS, new_event_status="Planing-stage")
    client._client = httpx.AsyncClient(
        base_url="https://api.notion.com/v1",
        transport=httpx.MockTransport(fake.handler),
        event_hooks={"request": [_refuse_deletion]},
    )
    return client


def test_slot_columns_match_the_plan() -> None:
    assert [slots.column_name(i) for i in range(6)] == [
        "21:00 - 22:00", "22:00 - 23:00", "23:00 - 24:00",
        "24:00 - 01:00", "01:00 - 02:00", "02:00 - 03:00",
    ]
    assert all(slots.column_name(i) in PLAN_COLUMNS for i in range(6))


def test_title_is_found_by_type_whatever_its_name() -> None:
    assert props.title_text(plan_event()) == "Casual Event"
    with pytest.raises(IndexError):
        props.title_text({"properties": {"Event": {"type": "title", "title": []}}})
    with pytest.raises(KeyError):
        props.title_text({"properties": {"Date": {"type": "date"}}})


@pytest.mark.asyncio
async def test_hand_entries_book_slots_and_open_decks_are_ignored() -> None:
    event = plan_event({"21:00 - 22:00": "DJ Nova", "03:00 - 04:00": "Alex birthday"})
    await slots.annotate(None, None, [event])
    st = slots.state(event)
    assert st.total == 6
    assert st.booked == {0: 0}  # typed in by hand: booked, no Discord user
    assert st.names == {0: "DJ Nova"}
    assert st.open_indices == [1, 2, 3, 4, 5]  # 03-04 (open decks, notes) is no slot


@pytest.mark.asyncio
@pytest.mark.parametrize("status,bookable", [
    ("Planing-stage", True), ("Requires Host", True), ("Ready :)", False),
    ("Done", False), ("CANCLED", False), ("Replacement", False),
])
async def test_only_the_planning_phase_is_bookable(status: str, bookable: bool) -> None:
    event = plan_event(status=status)
    await slots.annotate(None, None, [event])
    assert slots.state(event).bookable is bookable
    assert bool(slots.state(event).open_indices) is bookable


@pytest.mark.asyncio
async def test_bot_booking_and_hand_entry_together() -> None:
    event = plan_event({"23:00 - 24:00": "Pulse"})
    with patch("bot.slots.get_booked_slots", new=AsyncMock(return_value={"evt": {1: 42}})), \
         patch("bot.slots.get_value", new=AsyncMock(return_value=None)):
        await slots.annotate(MagicMock(), 1, [event])
    st = slots.state(event)
    assert st.booked == {1: 42, 2: 0}
    assert st.open_indices == [0, 3, 4, 5]


@pytest.mark.asyncio
async def test_booking_writes_the_name_into_an_empty_slot() -> None:
    fake = FakePlan()
    await make_client(fake).write_booking("evt", 1, None, [], "Casual Event", 0, dj_name="DJ Rayla")
    assert fake.patches == [{"properties": {"22:00 - 23:00": {
        "rich_text": [{"type": "text", "text": {"content": "DJ Rayla"}}]
    }}}]


@pytest.mark.asyncio
async def test_booking_never_overwrites_a_name_typed_in_by_hand() -> None:
    fake = FakePlan({"22:00 - 23:00": "Echo Fox"})
    with pytest.raises(NotionSlotTakenError) as caught:
        await make_client(fake).write_booking("evt", 1, None, [], "E", 0, dj_name="DJ Rayla")
    assert caught.value.current == "Echo Fox"
    assert fake.patches == []


@pytest.mark.asyncio
async def test_the_same_name_typed_in_by_hand_is_not_taken_over() -> None:
    """Treating it as the bot's own entry would let a later cancel empty the organizer's text."""
    fake = FakePlan({"22:00 - 23:00": "DJ Rayla"})
    with pytest.raises(NotionSlotTakenError):
        await make_client(fake).write_booking("evt", 1, None, [], "E", 0, dj_name="DJ Rayla")
    assert fake.patches == []


@pytest.mark.asyncio
async def test_a_retry_after_an_applied_patch_does_not_write_twice() -> None:
    """A 502 can arrive after Notion applied the PATCH; the retry finds the bot's own name."""
    fake = FakePlan()
    calls = {"n": 0}
    original = fake.handler

    async def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and calls["n"] == 0:
            calls["n"] += 1
            await original(request)  # applied ...
            return httpx.Response(502, json={})  # ... but answered with an error
        return await original(request)

    fake.handler = flaky
    client = make_client(fake)
    with patch("bot.notion.client.asyncio.sleep", new=AsyncMock()):
        await client.write_booking("evt", 1, None, [], "E", 0, dj_name="DJ Rayla")
    assert len(fake.patches) == 1
    assert props.plain_text(fake.page["properties"]["22:00 - 23:00"]) == "DJ Rayla"


@pytest.mark.asyncio
async def test_self_booking_refused_when_the_event_left_planning() -> None:
    from bot.notion.client import NotionNotBookableError

    fake = FakePlan()
    fake.page["properties"]["Status"]["status"]["name"] = "CANCLED"
    with pytest.raises(NotionNotBookableError):
        await make_client(fake).write_booking(
            "evt", 1, None, [], "E", 0, dj_name="DJ Rayla", require_bookable=True
        )
    assert fake.patches == []


@pytest.mark.asyncio
async def test_admin_book_replaces_only_the_displaced_dj() -> None:
    fake = FakePlan({"22:00 - 23:00": "Old DJ"})
    await make_client(fake).write_booking(
        "evt", 1, None, [], "E", 0, dj_name="New DJ", replaces="Old DJ"
    )
    assert props.plain_text(fake.page["properties"]["22:00 - 23:00"]) == "New DJ"


@pytest.mark.asyncio
async def test_cancel_clears_only_the_bots_own_name() -> None:
    fake = FakePlan({"22:00 - 23:00": "DJ Rayla", "23:00 - 24:00": "DJ Rayla (b2b Alex)"})
    client = make_client(fake)
    await client.delete_booking("evt", None, [], "E", 0, slot_index=1, dj_name="DJ Rayla")
    await client.delete_booking("evt", None, [], "E", 0, slot_index=2, dj_name="DJ Rayla")
    assert props.plain_text(fake.page["properties"]["22:00 - 23:00"]) == ""
    # changed by an organizer: stays
    assert props.plain_text(fake.page["properties"]["23:00 - 24:00"]) == "DJ Rayla (b2b Alex)"
    assert len(fake.patches) == 1


@pytest.mark.asyncio
async def test_no_dj_pages_in_the_plan() -> None:
    fake = FakePlan()
    assert await make_client(fake).write_dj_record(42, "DJ Rayla") is None
    assert fake.created == []


@pytest.mark.asyncio
async def test_new_event_uses_the_plans_title_and_starts_in_planning() -> None:
    fake = FakePlan()
    await make_client(fake).create_event("Casual Event", dt.date(2026, 12, 5), slots=6)
    created = fake.created[0]["properties"]
    assert created["Event"]["title"][0]["text"]["content"] == "Casual Event"
    assert created["Status"] == {"status": {"name": "Planing-stage"}}
    assert "Slots" not in created and "Name" not in created


@pytest.mark.asyncio
async def test_required_columns_for_the_plan() -> None:
    required = await slots.required_properties(None, None)
    assert list(required) == ["Date", "21:00 - 22:00", "22:00 - 23:00", "23:00 - 24:00",
                              "24:00 - 01:00", "01:00 - 02:00", "02:00 - 03:00", "Status"]
    assert required["Date"] == ("date",)
    assert required["21:00 - 22:00"] == ("rich_text",)
    assert required["Status"] == ("status", "select")


@pytest.mark.asyncio
async def test_required_columns_without_status_filters_have_no_status() -> None:
    slots.configure(slots.TDS)
    required = await slots.required_properties(None, None)
    assert "Status" not in required


@pytest.mark.asyncio
async def test_schema_check_reports_a_slot_column_of_the_wrong_type() -> None:
    fake = FakePlan()

    async def schema(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"properties": {
            "Date": {"type": "date"}, "21:00 - 22:00": {"type": "number"},
        }})

    fake.handler = schema
    client = make_client(fake)
    await client.validate_schema({"Date": ("date",), "21:00 - 22:00": ("rich_text",)})
    client._log.post_error.assert_awaited_once()
    trigger = client._log.post_error.call_args.kwargs["trigger"]
    assert "'21:00 - 22:00'" in trigger and "'number'" in trigger


@pytest.mark.asyncio
async def test_reconcile_reports_a_booking_missing_from_the_plan_not_hand_entries() -> None:
    from bot.reconcile import find_mismatches

    event = plan_event({"21:00 - 22:00": "DJ Nova", "22:00 - 23:00": ""})
    with patch("bot.reconcile.get_booked_slots", new=AsyncMock(return_value={"evt": {1: 42}})), \
         patch("bot.reconcile.get_plan_texts", new=AsyncMock(return_value={})), \
         patch("bot.reconcile.get_dj_names", new=AsyncMock(return_value={42: "DJ Rayla"})):
        found = await find_mismatches(MagicMock(), [event])
    assert found == [{"event": "Casual Event", "missing_in_notion": ["slot 2: DJ Rayla"],
                      "only_in_notion": []}]


@pytest.mark.asyncio
async def test_reconcile_is_quiet_when_plan_and_bookings_agree() -> None:
    from bot.reconcile import find_mismatches

    event = plan_event({"22:00 - 23:00": "DJ Rayla", "01:00 - 02:00": "Lumen"})
    with patch("bot.reconcile.get_booked_slots", new=AsyncMock(return_value={"evt": {1: 42}})), \
         patch("bot.reconcile.get_plan_texts", new=AsyncMock(return_value={})), \
         patch("bot.reconcile.get_dj_names", new=AsyncMock(return_value={42: "DJ Rayla"})):
        assert await find_mismatches(MagicMock(), [event]) == []


@pytest.mark.asyncio
async def test_reconcile_compares_with_the_text_the_bot_wrote() -> None:
    """After a rename the plan still shows the old text the bot wrote: no false alarm."""
    from bot.reconcile import find_mismatches

    event = plan_event({"22:00 - 23:00": "Old Name"})
    with patch("bot.reconcile.get_booked_slots", new=AsyncMock(return_value={"evt": {1: 42}})), \
         patch("bot.reconcile.get_plan_texts",
               new=AsyncMock(return_value={("evt", 1): "Old Name"})), \
         patch("bot.reconcile.get_dj_names", new=AsyncMock(return_value={42: "New Name"})):
        assert await find_mismatches(MagicMock(), [event]) == []


def _service_with(events: list[dict]):
    from bot.services.admin_booking import AdminBookingService

    return AdminBookingService(bot=MagicMock(), db=MagicMock(), notion_client=None,
                               log_service=None), events


@pytest.mark.parametrize("query", ["2026-11-21", "21.11.2026"])
def test_admin_finds_the_event_by_date(query: str) -> None:
    svc, _ = _service_with([])
    first, second = plan_event(event_id="a"), plan_event(event_id="b")
    second["properties"]["Date"]["date"]["start"] = "2026-11-21"
    event, problem = svc._find_event([first, second], query)
    assert event is second and problem == ""


def test_admin_refuses_a_name_shared_by_several_events() -> None:
    svc, _ = _service_with([])
    first, second = plan_event(event_id="a"), plan_event(event_id="b")
    second["properties"]["Date"]["date"]["start"] = "2026-11-21"
    event, problem = svc._find_event([first, second], "casual event")
    assert event is None
    assert "2026-10-24" in problem and "2026-11-21" in problem


def test_admin_refuses_two_events_on_one_day() -> None:
    svc, _ = _service_with([])
    event, problem = svc._find_event([plan_event(event_id="a"), plan_event(event_id="b")],
                                     "2026-10-24")
    assert event is None and "duplicate" in problem


def test_layout_and_statuses_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.config import Config

    for key, value in {"DISCORD_BOT_TOKEN": "t", "POSTGRES_URL": "p", "NOTION_API_KEY": "k",
                       "NOTION_DATABASE_ID": "d", "ADMIN_USER_IDS": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NOTION_LAYOUT", "TDS")
    monkeypatch.setenv("NOTION_BOOKABLE_STATUSES", "Planing-stage, Not started,Requires Host")
    monkeypatch.setenv("NOTION_CANCELLED_STATUSES", "CANCLED, CANCLED (moved)")
    config = Config.from_env()
    assert config.notion_layout == "tds"
    assert config.notion_bookable_statuses == ("Planing-stage", "Not started", "Requires Host")
    assert config.notion_cancelled_statuses == ("CANCLED", "CANCLED (moved)")
    monkeypatch.setenv("NOTION_LAYOUT", "sheet")
    with pytest.raises(SystemExit):
        Config.from_env()


@pytest.mark.asyncio
async def test_misspelt_status_names_are_reported_at_startup() -> None:
    fake = FakePlan()

    async def schema(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"properties": {"Status": {"type": "status", "status": {
            "options": [{"name": "Planing-stage"}, {"name": "CANCLED"}]}}}})

    fake.handler = schema
    client = make_client(fake)
    await client.validate_status_names(["Planning-stage", "CANCLED"])
    client._log.post_error.assert_awaited_once()
    assert "Planning-stage" in client._log.post_error.call_args.kwargs["trigger"]
    client._log.post_error.reset_mock()
    await client.validate_status_names(["Planing-stage", "CANCLED"])
    client._log.post_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_schema_check_ignores_whitespace_like_the_writes() -> None:
    fake = FakePlan()

    async def schema(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"properties": {"21:00-22:00": {"type": "rich_text"}}})

    fake.handler = schema
    client = make_client(fake)
    await client.validate_schema({"21:00 - 22:00": ("rich_text",)})
    client._log.post_error.assert_not_awaited()
