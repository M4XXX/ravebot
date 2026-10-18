"""Story 9.1: bot.slots / bot.schedule module state cannot leak from one test to the next.

The two tests run in file order: the first changes the state without cleaning up, the
second checks that the autouse fixture in tests/conftest.py has reset it.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from bot import schedule, slots


def test_a_changes_the_layout_and_schedule_without_cleanup() -> None:
    slots.configure(slots.TDS, frozenset({"Planing-stage"}), frozenset({"CANCLED"}))
    schedule._current = schedule.Schedule(
        tz=ZoneInfo("Europe/Berlin"), start=dt.time(21), slot_minutes=30
    )


def test_b_starts_with_the_defaults_again() -> None:
    assert slots.layout() == slots.CLASSIC
    assert slots.bookable_statuses() == frozenset()
    assert schedule.current() == schedule.DEFAULT_SCHEDULE
