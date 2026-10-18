"""Drift between Notion and slot_locks is reported once and never fixed automatically."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import reconcile
from bot.ui.embeds import ErrorCode


def _event(event_id: str, *linked: str) -> dict:
    return {
        "id": event_id,
        "properties": {
            "Name": {"title": [{"plain_text": f"Rave {event_id}"}]},
            "DJs": {"relation": [{"id": pid} for pid in linked]},
        },
    }


def _patches(booked: dict, pages: dict, last: str | None = None):
    store = {"value": last}

    async def get_value(pool, gid, key):
        return store["value"]

    async def set_value(pool, gid, key, value):
        store["value"] = value

    return store, (
        patch("bot.reconcile.get_booked_slots", new=AsyncMock(return_value=booked)),
        patch("bot.reconcile.get_notion_page_ids", new=AsyncMock(return_value=pages)),
        patch("bot.reconcile.get_value", new=get_value),
        patch("bot.reconcile.set_value", new=set_value),
    )


@pytest.mark.asyncio
async def test_finds_both_directions_and_ignores_dashes() -> None:
    events = [
        _event("a", "aaaa-1111"),           # booked DJ 1 linked (dashes differ) -> fine
        _event("b"),                         # DJ 2 booked but not linked
        _event("c", "cccc-9999"),            # linked by hand, nobody booked
    ]
    booked = {"a": {0: 1}, "b": {2: 2}}
    pages = {1: "aaaa1111", 2: "bbbb2222"}
    _, ps = _patches(booked, pages)
    with ps[0], ps[1]:
        found = await reconcile.find_mismatches(MagicMock(), events)
    assert found == [
        {"event": "Rave b", "missing_in_notion": ["bbbb2222"], "only_in_notion": []},
        {"event": "Rave c", "missing_in_notion": [], "only_in_notion": ["cccc9999"]},
    ]


@pytest.mark.asyncio
async def test_reports_once_per_distinct_drift_and_never_writes() -> None:
    log = AsyncMock()
    pool = MagicMock()
    events = [_event("b")]
    store, ps = _patches({"b": {0: 2}}, {2: "bbbb2222"})
    with ps[0], ps[1], ps[2], ps[3]:
        await reconcile.report(pool, 42, events, log)
        await reconcile.report(pool, 42, events, log)
    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_DATA_MISMATCH
    assert "Rave b" in log.post_error.call_args.kwargs["trigger"]
    assert pool.mock_calls == []  # only the patched read helpers touched the database


@pytest.mark.asyncio
async def test_agreement_posts_nothing() -> None:
    log = AsyncMock()
    _, ps = _patches({"a": {0: 1}}, {1: "aaaa1111"})
    with ps[0], ps[1], ps[2], ps[3]:
        found = await reconcile.report(MagicMock(), 42, [_event("a", "aaaa1111")], log)
    assert found == []
    log.post_error.assert_not_called()


@pytest.mark.asyncio
async def test_events_with_a_truncated_relation_are_not_compared() -> None:
    """Notion cuts the relation in query results at 25 pages and sets has_more."""
    event = _event("big", *[f"page{i:04d}" for i in range(25)])
    event["properties"]["DJs"]["has_more"] = True
    booked = {"big": {i: i for i in range(26)}}
    pages = {i: f"page{i:04d}" for i in range(26)}  # DJ 25 is beyond the cut
    _, ps = _patches(booked, pages)
    with ps[0], ps[1]:
        assert await reconcile.find_mismatches(MagicMock(), [event]) == []
