"""Tests for bot/schedule.py — event and slot start times from date, start, length and zone."""
import datetime as dt
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from bot import schedule
from bot.services.reminders import RemindersService

BERLIN = schedule.Schedule(tz=ZoneInfo("Europe/Berlin"), start=dt.time(21, 0), slot_minutes=60)


def _event(date_str: str) -> dict:
    return {"id": "evt-1", "properties": {"Date": {"date": {"start": date_str}}}}


def _utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


def test_default_schedule_is_20_utc() -> None:
    assert schedule.event_start(_event("2026-10-24"), schedule.DEFAULT_SCHEDULE) == _utc(
        2026, 10, 24, 20, 0
    )


def test_local_start_follows_dst() -> None:
    """21:00 Berlin is 19:00 UTC in summer time and 20:00 UTC in winter time."""
    assert schedule.event_start(_event("2026-10-24"), BERLIN) == _utc(2026, 10, 24, 19, 0)
    assert schedule.event_start(_event("2026-10-31"), BERLIN) == _utc(2026, 10, 31, 20, 0)


def test_slots_follow_start_and_length() -> None:
    sched = schedule.Schedule(tz=ZoneInfo("UTC"), start=dt.time(20, 0), slot_minutes=90)
    assert schedule.slot_start(_event("2026-10-24"), 0, sched) == _utc(2026, 10, 24, 20, 0)
    assert schedule.slot_start(_event("2026-10-24"), 2, sched) == _utc(2026, 10, 24, 23, 0)


def test_slots_keep_wall_clock_across_midnight() -> None:
    """Slot 3 of a 21:00 Berlin event is midnight Berlin time."""
    local = schedule.slot_start(_event("2026-10-31"), 3, BERLIN).astimezone(BERLIN.tz)
    assert (local.day, local.hour, local.minute) == (1, 0, 0)


@pytest.mark.parametrize("night", ["2027-03-27", "2026-10-24"])  # clocks forward / back
def test_slots_are_evenly_spaced_on_the_night_the_clocks_change(night: str) -> None:
    sched = schedule.Schedule(tz=ZoneInfo("Europe/Berlin"), start=dt.time(22, 0), slot_minutes=60)
    starts = [schedule.slot_unix(_event(night), i, sched) for i in range(7)]
    assert [b - a for a, b in zip(starts, starts[1:])] == [3600] * 6
    first = schedule.slot_start(_event(night), 0, sched).astimezone(sched.tz)
    assert (first.hour, first.minute) == (22, 0)


def test_date_with_time_is_used_as_entered() -> None:
    assert schedule.event_start(_event("2026-10-24T18:30:00+00:00"), BERLIN) == _utc(
        2026, 10, 24, 18, 30
    )


def test_date_with_naive_time_uses_configured_zone() -> None:
    assert schedule.event_start(_event("2026-10-24T22:00:00"), BERLIN) == _utc(2026, 10, 24, 20, 0)


@pytest.mark.parametrize("event", [{}, {"properties": {}}, _event("not-a-date")])
def test_unparseable_date_gives_none_and_zero(event: dict) -> None:
    assert schedule.event_start(event, BERLIN) is None
    assert schedule.slot_unix(event, 1, BERLIN) == 0


def test_parse_schedule_falls_back_per_field() -> None:
    sched = schedule.parse_schedule("Mars/Olympus", "25:99", "0")
    assert sched == schedule.DEFAULT_SCHEDULE
    sched = schedule.parse_schedule("Europe/Berlin", "21:00", None)
    assert (sched.tz.key, sched.start, sched.slot_minutes) == ("Europe/Berlin", dt.time(21, 0), 60)


def test_parse_timezone() -> None:
    assert schedule.parse_timezone(" Europe/Berlin ").key == "Europe/Berlin"
    assert schedule.parse_timezone("Nowhere/Land") is None
    assert schedule.parse_timezone("") is None


@pytest.mark.asyncio
async def test_reminder_slot_time_matches_booking_slot_time() -> None:
    """Regression: the booking view and the reminder must show the same slot time.

    Booking used event midnight UTC + slot_index hours, reminders used the configured start
    time — a DJ booked "02:00" and was reminded for 20:00 UTC.
    """
    config = {
        "timezone": "Europe/Berlin",
        "default_start_time": "21:00",
        "slot_duration_minutes": "60",
    }

    async def fake_get_value(pool, guild_id, key):
        return config.get(key)

    svc = RemindersService(
        bot=AsyncMock(), db=AsyncMock(), notion_client=AsyncMock(), log_service=None
    )
    event = _event("2026-10-24")
    with patch("bot.schedule.get_value", new=fake_get_value):
        reminder_unix = await svc._compute_slot_unix(event, 2, guild_id=1)
    booking_unix = schedule.slot_unix(event, 2)  # cached schedule after the refresh above
    assert reminder_unix == booking_unix == int(_utc(2026, 10, 24, 21, 0).timestamp())


def test_plain_label_uses_configured_zone() -> None:
    unix = int(_utc(2026, 10, 24, 19, 0).timestamp())
    assert schedule.plain_label(unix, BERLIN) == "Sat 24 Oct 21:00 CEST"
    assert "<t:" not in schedule.plain_label(unix, BERLIN)


# Story 8.7: "today" is the organizers' date, not the server's


def test_today_uses_the_configured_time_zone(monkeypatch) -> None:
    import datetime as dt
    from zoneinfo import ZoneInfo

    from bot import schedule

    utc_now = dt.datetime(2026, 10, 24, 23, 30, tzinfo=dt.UTC)

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_now.astimezone(tz)

    monkeypatch.setattr(schedule.dt, "datetime", Frozen)
    ahead = schedule.Schedule(tz=ZoneInfo("Pacific/Kiritimati"), start=dt.time(21), slot_minutes=60)
    behind = schedule.Schedule(tz=ZoneInfo("Pacific/Pago_Pago"), start=dt.time(21), slot_minutes=60)
    assert schedule.today(ahead) == dt.date(2026, 10, 25)   # UTC+14
    assert schedule.today(behind) == dt.date(2026, 10, 24)  # UTC-11
    monkeypatch.setattr(schedule, "_current", ahead)
    assert schedule.today() == dt.date(2026, 10, 25)


def test_no_server_local_dates_left_in_the_bot() -> None:
    """Every "today" goes through schedule.today() (poll, lineup, auto-creation, /add-event)."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "bot"
    offenders = [
        f"{path.relative_to(root.parent)}:{n}"
        for path in root.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\bdate\.today\(\)", line)
    ]
    assert offenders == []


async def test_poll_events_asks_from_the_organizers_today(monkeypatch) -> None:
    from unittest.mock import MagicMock

    import httpx

    from bot import schedule
    from bot.notion.client import NotionClient

    monkeypatch.setattr(schedule, "today", lambda sched=None: dt.date(2026, 10, 25))
    client = NotionClient(api_key="k", database_id="db", log_service=MagicMock())
    client._client.post = AsyncMock(return_value=httpx.Response(
        200, json={"results": [], "has_more": False}, request=httpx.Request("POST", "https://x")
    ))
    await client.poll_events()
    date_filter = client._client.post.call_args.kwargs["json"]["filter"]["and"][0]
    assert date_filter["date"]["on_or_after"] == "2026-10-25"
