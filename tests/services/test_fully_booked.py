"""Tests for bot/services/fully_booked.py."""
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.fully_booked import check_fully_booked
from tests.notion.factories import make_event, make_slot


class FakeStore:
    """In-memory stand-in for the fully_booked_events table."""

    def __init__(self) -> None:
        self.rows: set[str] = set()

    def patch(self, monkeypatch) -> None:
        async def get_announced(pool, ids):
            return {i for i in ids if i in self.rows}

        async def mark_announced(pool, event_id):
            if event_id in self.rows:
                return False
            self.rows.add(event_id)
            return True

        async def clear_announced(pool, event_id):
            self.rows.discard(event_id)

        monkeypatch.setattr("bot.services.fully_booked.fb_db.get_announced", get_announced)
        monkeypatch.setattr("bot.services.fully_booked.fb_db.mark_announced", mark_announced)
        monkeypatch.setattr("bot.services.fully_booked.fb_db.clear_announced", clear_announced)


def _event(booked: int, count: int = 3, page_id: str = "ev-1"):
    slots = [
        make_slot(i, dj_page_id=f"dj-{i}", dj_name=f"DJ {i}") if i <= booked else make_slot(i)
        for i in range(1, count + 1)
    ]
    return make_event(page_id=page_id, slot_count=count, slots=slots)


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    s = FakeStore()
    s.patch(monkeypatch)
    return s


@pytest.fixture
def log() -> MagicMock:
    m = MagicMock()
    m.log_fully_booked = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_announces_full_event_once(store, log) -> None:
    event = _event(3)
    await check_fully_booked(MagicMock(), log, [event])
    await check_fully_booked(MagicMock(), log, [event])
    log.log_fully_booked.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_not_full_event_is_not_announced(store, log) -> None:
    await check_fully_booked(MagicMock(), log, [_event(2)])
    log.log_fully_booked.assert_not_awaited()
    assert store.rows == set()


@pytest.mark.asyncio
async def test_clears_when_slot_opens_and_announces_again_after_refill(store, log) -> None:
    await check_fully_booked(MagicMock(), log, [_event(3)])
    await check_fully_booked(MagicMock(), log, [_event(2)])
    assert store.rows == set()
    await check_fully_booked(MagicMock(), log, [_event(3)])
    assert log.log_fully_booked.await_count == 2


@pytest.mark.asyncio
async def test_event_without_slots_is_ignored(store, log) -> None:
    await check_fully_booked(MagicMock(), log, [make_event(slot_count=0, slots=[])])
    log.log_fully_booked.assert_not_awaited()
    assert store.rows == set()


@pytest.mark.asyncio
async def test_duplicate_slot_rows_resolve_by_lowest_page_id(store, log) -> None:
    # Slot 2 has two rows; the lowest page id (empty) is the one that counts.
    slots = [
        make_slot(1, dj_page_id="d1"),
        make_slot(2, page_id="a-row"),
        make_slot(2, page_id="b-row", dj_page_id="d2"),
    ]
    await check_fully_booked(MagicMock(), log, [make_event(slot_count=2, slots=slots)])
    log.log_fully_booked.assert_not_awaited()
    slots = [
        make_slot(1, dj_page_id="d1"),
        make_slot(2, page_id="a-row", dj_page_id="d2"),
        make_slot(2, page_id="b-row"),
    ]
    await check_fully_booked(MagicMock(), log, [make_event(slot_count=2, slots=slots)])
    log.log_fully_booked.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_slot_number_is_not_full(store, log) -> None:
    slots = [make_slot(1, dj_page_id="d1"), make_slot(3, dj_page_id="d3")]
    await check_fully_booked(MagicMock(), log, [make_event(slot_count=3, slots=slots)])
    log.log_fully_booked.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_paths_filling_at_once_announce_once(store, log) -> None:
    import asyncio

    event = _event(3)
    await asyncio.gather(
        check_fully_booked(MagicMock(), log, [event]),
        check_fully_booked(MagicMock(), log, [event]),
    )
    log.log_fully_booked.assert_awaited_once()


@pytest.mark.asyncio
async def test_failing_log_service_is_swallowed(store) -> None:
    log = MagicMock()
    log.log_fully_booked = AsyncMock(side_effect=RuntimeError("boom"))
    await check_fully_booked(MagicMock(), log, [_event(3)])


@pytest.mark.asyncio
async def test_failing_database_is_swallowed(log) -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    await check_fully_booked(pool, log, [_event(3)])
    log.log_fully_booked.assert_not_awaited()


def test_removed_transcript_api_is_gone() -> None:
    from bot.log import AdminLogService

    for name in ("post_dm_transcript", "post_interaction", "post_admin_override"):
        assert not hasattr(AdminLogService, name)
    import bot.ui.embeds as embeds

    assert not hasattr(embeds, "AdminLogEmbed")
    assert not hasattr(embeds, "ErrorEmbed")
    root = Path(__file__).parents[2] / "bot"
    pattern = re.compile(
        r"post_dm_transcript|post_interaction|post_admin_override|AdminLogEmbed|ErrorEmbed"
    )
    for path in root.rglob("*.py"):
        assert not pattern.search(path.read_text()), path
