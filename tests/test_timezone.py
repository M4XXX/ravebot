"""Time zone core: local wall-clock events, DST-safe slots, local text."""
import datetime
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import timezone as tz
from bot.notion.models import EventInfo
from bot.ui import texts


@pytest.fixture(autouse=True)
def _reset_zone():
    tz.set_timezone(tz.DEFAULT_TIMEZONE)
    yield
    tz.set_timezone(tz.DEFAULT_TIMEZONE)


def _event(date: datetime.date, start=datetime.time(21, 0), minutes=60) -> EventInfo:
    return EventInfo(
        page_id="p", event_id="E", date=date, status=None, genre=None, notes=None,
        slot_count=6, start=start, slot_minutes=minutes, slots=(),
    )


def _times(event: EventInfo, n: int) -> list[str]:
    return [event.slot_start(i).strftime("%H:%M") for i in range(n)]


def test_winter_21_is_utc_20() -> None:
    s = _event(datetime.date(2026, 11, 14)).slot_start(0)
    assert s.strftime("%H:%M") == "21:00"
    assert s.astimezone(datetime.UTC).strftime("%H:%M") == "20:00"
    assert s.tzinfo is not None


def test_summer_21_is_utc_19() -> None:
    s = _event(datetime.date(2026, 7, 14)).slot_start(0)
    assert s.strftime("%H:%M") == "21:00"
    assert s.astimezone(datetime.UTC).strftime("%H:%M") == "19:00"


def test_dst_spring_switch_crossing_midnight() -> None:
    # 2026-03-29 02:00 -> 03:00; slots from the evening of the 28th
    assert _times(_event(datetime.date(2026, 3, 28)), 6) == [
        "21:00", "22:00", "23:00", "00:00", "01:00", "03:00",
    ]
    # Start 00:30 on the switch day: real minutes jump over the gap
    assert _times(_event(datetime.date(2026, 3, 29), datetime.time(0, 30)), 4) == [
        "00:30", "01:30", "03:30", "04:30",
    ]


def test_dst_autumn_switch_crossing_midnight() -> None:
    # 2026-10-25 03:00 -> 02:00 (clock repeats 02:00 hour)
    assert _times(_event(datetime.date(2026, 10, 24)), 6) == [
        "21:00", "22:00", "23:00", "00:00", "01:00", "02:00",
    ]
    s = [_event(datetime.date(2026, 10, 25), datetime.time(0, 30)).slot_start(i) for i in range(5)]
    gaps = {(b.astimezone(datetime.UTC) - a.astimezone(datetime.UTC)) for a, b in zip(s, s[1:])}
    assert gaps == {datetime.timedelta(minutes=60)}


def test_text_helpers_are_local() -> None:
    e = _event(datetime.date(2026, 7, 14), datetime.time(19, 0))
    assert texts.fmt_time(e, 0) == "19:00"
    assert texts.fmt_time(e, 2) == "21:00"
    assert texts.slot_phrase(e, 1) == "slot 2 at 20:00"
    assert texts.fmt_slot_when(e, 5) == "Wednesday 15.07.2026, 00:00"
    assert texts.fmt_slot_when(e, 0) == "Tuesday 14.07.2026, 19:00"


def test_set_timezone_invalid_keeps_zone() -> None:
    with pytest.raises(ValueError):
        tz.set_timezone("Nope/Zone")
    assert tz.get_timezone().key == "Europe/Berlin"
    assert not tz.is_valid_timezone("Nope/Zone")
    assert tz.is_valid_timezone("America/New_York")


def test_set_timezone_changes_slot_start() -> None:
    tz.set_timezone("UTC")
    assert _event(datetime.date(2026, 7, 14)).slot_start(0).utcoffset() == datetime.timedelta(0)


def test_local_today_uses_zone() -> None:
    fixed = datetime.datetime(2026, 11, 7, 23, 30, tzinfo=datetime.UTC)  # 00:30 on 8th in Berlin

    class _DT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return fixed.astimezone(tz) if tz else fixed

    with patch.object(tz.datetime, "datetime", _DT):
        assert tz.local_today() == datetime.date(2026, 11, 8)
        assert fixed.date() == datetime.date(2026, 11, 7)
        tz.set_timezone("UTC")
        assert tz.local_today() == datetime.date(2026, 11, 7)


@pytest.mark.asyncio
async def test_load_stored_timezone() -> None:
    with patch("bot.db.config.get_value", new=AsyncMock(return_value="America/New_York")):
        assert await tz.load_stored_timezone(MagicMock(), 1) is True
    assert tz.get_timezone().key == "America/New_York"


@pytest.mark.asyncio
async def test_load_stored_timezone_invalid_warns(caplog) -> None:  # noqa: ANN001
    with patch("bot.db.config.get_value", new=AsyncMock(return_value="Nope/Zone")):
        assert await tz.load_stored_timezone(MagicMock(), 1) is False
    assert tz.get_timezone().key == "Europe/Berlin"
    assert "invalid" in caplog.text


def test_reminder_window_uses_real_instant() -> None:
    from bot.services.reminders import RemindersService

    e = _event(datetime.date(2026, 11, 14))  # 20:00 UTC
    svc = RemindersService.__new__(RemindersService)
    now = datetime.datetime(2026, 11, 13, 20, 0, tzinfo=datetime.UTC)  # 24h before
    assert svc._is_in_reminder_window(e, now) is True
    assert svc._is_in_reminder_window(e, now - datetime.timedelta(hours=5)) is False


def test_no_discord_timestamps_in_bot_source() -> None:
    root = Path(__file__).resolve().parent.parent / "bot"
    offenders = [
        str(p) for p in root.rglob("*.py")
        if re.search(r"<t:[^>\s]*[{\d]", p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
