"""Tests for bot/services/event_defaults.py — default shape of new events from config."""
import datetime
import logging
from unittest.mock import AsyncMock, patch

import pytest

from bot.services.event_defaults import EventShape, default_event_shape


def _cfg(values: dict[str, str | None]):
    async def fake_get_value(pool, guild_id, key):
        return values.get(key)

    return patch("bot.services.event_defaults.get_value", new=AsyncMock(side_effect=fake_get_value))


@pytest.mark.asyncio
async def test_defaults_when_unset() -> None:
    with _cfg({}):
        shape = await default_event_shape(object(), 1)
    assert shape == EventShape(start=datetime.time(20, 0), slot_minutes=60, slot_count=6)


@pytest.mark.asyncio
async def test_reads_configured_values() -> None:
    values = {
        "default_start_time": "18:30",
        "slot_duration_minutes": "45",
        "slots_per_event": "4",
    }
    with _cfg(values) as get:
        shape = await default_event_shape(object(), 7)
    assert shape == EventShape(start=datetime.time(18, 30), slot_minutes=45, slot_count=4)
    assert {c.args[1] for c in get.await_args_list} == {7}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_time", ["abc", "25:00", "20:60", "20", "", "-1:00"])
async def test_invalid_start_time_falls_back(
    bad_time: str, caplog: pytest.LogCaptureFixture
) -> None:
    with _cfg({"default_start_time": bad_time}), caplog.at_level(logging.WARNING):
        shape = await default_event_shape(object(), 1)
    assert shape.start == datetime.time(20, 0)
    if bad_time:
        assert any("default_start_time" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "0", "-3", "1.5"])
async def test_invalid_or_non_positive_numbers_fall_back(
    bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    values = {"slot_duration_minutes": bad, "slots_per_event": bad}
    with _cfg(values), caplog.at_level(logging.WARNING):
        shape = await default_event_shape(object(), 1)
    assert shape.slot_minutes == 60
    assert shape.slot_count == 6
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "slot_duration_minutes" in messages
    assert "slots_per_event" in messages


@pytest.mark.asyncio
async def test_slots_per_event_above_100_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    with _cfg({"slots_per_event": "101"}), caplog.at_level(logging.WARNING):
        shape = await default_event_shape(object(), 1)
    assert shape.slot_count == 6
    assert "slots_per_event" in " ".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_slots_per_event_100_is_kept() -> None:
    with _cfg({"slots_per_event": "100"}):
        shape = await default_event_shape(object(), 1)
    assert shape.slot_count == 100
