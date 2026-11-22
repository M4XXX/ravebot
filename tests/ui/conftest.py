import datetime

import pytest


@pytest.fixture(autouse=True)
def _clock_before_test_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """View tests use events in July 2026: keep the rules clock well before them.

    Tests of the cancellation cutoff patch bot.policy.utc_now themselves.
    """
    monkeypatch.setattr(
        "bot.policy.utc_now",
        lambda: datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
    )
