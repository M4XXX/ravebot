"""Slot occupancy comes from slot_locks by index; the slot count from Notion or /config."""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import slots
from bot.db.bookings import get_booked_slots

pytestmark = pytest.mark.real_slots


def _event(event_id: str = "evt", slots_value: int | None = None, linked: int = 0) -> dict:
    return {
        "id": event_id,
        "properties": {
            "Name": {"title": [{"plain_text": "Rave"}]},
            "Date": {"date": {"start": "2026-10-24"}},
            "Slots": {"number": slots_value},
            "DJs": {"relation": [{"id": f"p{i}"} for i in range(linked)]},
        },
    }


def _pool(rows: list[dict]) -> MagicMock:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


@pytest.mark.asyncio
async def test_get_booked_slots_groups_by_event() -> None:
    pool = _pool([
        {"event_id": "a", "slot_index": 3, "discord_user_id": 11},
        {"event_id": "a", "slot_index": 0, "discord_user_id": 12},
        {"event_id": "b", "slot_index": 1, "discord_user_id": 13},
    ])
    assert await get_booked_slots(pool, ["a", "b", "c"]) == {"a": {3: 11, 0: 12}, "b": {1: 13}}
    assert await get_booked_slots(pool, []) == {}


@pytest.mark.asyncio
async def test_booked_slot_is_known_by_index_not_by_count() -> None:
    """A DJ holds Slot 4: Slot 4 is taken, Slot 1 is still free (Repro A)."""
    from bot.ui.embeds import MissionOverviewEmbed

    event = _event(linked=1)
    pool = _pool([{"event_id": "evt", "slot_index": 3, "discord_user_id": 11}])
    with patch("bot.slots.get_value", new=AsyncMock(return_value=None)):
        await slots.annotate(pool, 42, [event])
    state = slots.state(event)
    assert state.open_indices == [0, 1, 2, 4, 5]
    fields = {f.name: f.value for f in MissionOverviewEmbed(event).fields}
    assert fields["Slot 4"].startswith("✓ Confirmed")
    assert fields["Slot 1"].startswith("Open")


@pytest.mark.asyncio
async def test_slot_count_from_notion_then_config_then_default() -> None:
    pool = _pool([])
    with patch("bot.slots.get_value", new=AsyncMock(return_value="5")):
        own, configured = _event("a", slots_value=4), _event("b", slots_value=None)
        await slots.annotate(pool, 42, [own, configured])
    assert slots.state(own).total == 4
    assert slots.state(configured).total == 5
    with patch("bot.slots.get_value", new=AsyncMock(return_value=None)):
        fallback = _event("c")
        await slots.annotate(pool, 42, [fallback])
    assert slots.state(fallback).total == slots.DEFAULT_SLOTS


@pytest.mark.asyncio
async def test_explicit_zero_slots_means_no_slots() -> None:
    event = _event(slots_value=0)
    with patch("bot.slots.get_value", new=AsyncMock(return_value="6")):
        await slots.annotate(_pool([]), 42, [event])
    assert slots.state(event).total == 0
    assert slots.state(event).open_indices == []


@pytest.mark.asyncio
async def test_booking_list_hides_fully_booked_events() -> None:
    from bot.ui.views import _filter_events_with_open_slots

    full, partly = _event("full", slots_value=2), _event("partly", slots_value=2)
    pool = _pool([
        {"event_id": "full", "slot_index": 0, "discord_user_id": 1},
        {"event_id": "full", "slot_index": 1, "discord_user_id": 2},
        {"event_id": "partly", "slot_index": 1, "discord_user_id": 3},
    ])
    with patch("bot.slots.get_value", new=AsyncMock(return_value=None)):
        await slots.annotate(pool, 42, [full, partly])
    assert [e["id"] for e in _filter_events_with_open_slots([full, partly])] == ["partly"]
    assert slots.state(partly).open_indices == [0]


@pytest.mark.asyncio
async def test_create_event_writes_slot_count() -> None:
    from bot.notion.client import NotionClient

    client = NotionClient(api_key="secret_test", database_id="db", log_service=MagicMock())
    client._write_with_retry = AsyncMock(return_value={"id": "page-1"})
    await client.create_event("Rave", datetime.date(2026, 10, 24), slots=6)
    payload = client._write_with_retry.call_args.kwargs["payload"]
    assert payload["properties"]["Slots"] == {"number": 6}


@pytest.mark.asyncio
async def test_classic_required_properties_are_the_one_constant() -> None:
    """Story 8.4: the classic name -> type map lives in slots.CLASSIC_PROPERTIES."""
    slots.configure(slots.CLASSIC)
    required = await slots.required_properties(None, None)
    assert required == slots.CLASSIC_PROPERTIES
    assert required is not slots.CLASSIC_PROPERTIES  # callers get a copy
    assert required == {
        "Name": ("title",), "Date": ("date",), "Slots": ("number",),
        "DJs": ("relation",), "Discord ID": ("rich_text",),
    }
