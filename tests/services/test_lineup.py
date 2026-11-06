"""Tests for bot/services/lineup.py — upcoming event, snapshot and LineupService."""
import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.services.lineup import (
    LINEUP_DEBOUNCE_SECONDS,
    LineupService,
    event_snapshot,
    future_events,
    upcoming_event,
)
from bot.ui.embeds import LineupUpdateEmbed
from tests.notion.factories import make_event, make_slot

TODAY = datetime.date(2026, 11, 6)
GUILD = 123456789


def _ev(page_id: str, date: datetime.date, **kw: object):
    return make_event(event_id=page_id, page_id=page_id, date=date, **kw)


class FakeConfig:
    def __init__(self, **values: str) -> None:
        self.values: dict[str, str | None] = {"dj_channel": "111", **values}

    async def get(self, db, guild_id, key):
        return self.values.get(key)

    async def set(self, db, guild_id, key, value):
        self.values[key] = value


def _service(
    cfg: FakeConfig | None = None,
    channel: MagicMock | None = None,
    events: list | None = None,
):
    cfg = cfg or FakeConfig()
    channel = channel if channel is not None else MagicMock(send=AsyncMock())
    bot = MagicMock()
    bot.guilds = [MagicMock(id=GUILD)]
    bot.get_channel = MagicMock(return_value=channel)
    log = MagicMock(post_error=AsyncMock(), log_attention=AsyncMock())
    notion = MagicMock(list_events=AsyncMock(return_value=events or []))
    service = LineupService(bot=bot, db=MagicMock(), log_service=log, notion_client=notion)
    return service, cfg, channel, log, notion


def _patched(cfg: FakeConfig):
    return (
        patch("bot.services.lineup.get_value", new=cfg.get),
        patch("bot.services.lineup.set_value", new=cfg.set),
        patch("bot.services.lineup._today", return_value=TODAY),
    )


class _Ctx:
    def __init__(self, cfg: FakeConfig) -> None:
        self.ps = _patched(cfg)

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *a):
        for p in self.ps:
            p.stop()


def test_debounce_constant() -> None:
    assert LINEUP_DEBOUNCE_SECONDS == 45


def test_upcoming_event_skips_done_canceled_and_past() -> None:
    events = [
        _ev("past", datetime.date(2026, 11, 5)),
        _ev("done", datetime.date(2026, 11, 6), status="Done"),
        _ev("canc", datetime.date(2026, 11, 7), status="Canceled"),
        _ev("later", datetime.date(2026, 11, 20)),
        _ev("next", datetime.date(2026, 11, 14)),
    ]
    assert upcoming_event(events, TODAY).page_id == "next"


def test_upcoming_event_includes_today_and_none() -> None:
    assert upcoming_event([_ev("t", TODAY)], TODAY).page_id == "t"
    assert upcoming_event([_ev("p", datetime.date(2026, 1, 1))], TODAY) is None


def test_future_events_sorted_and_filtered() -> None:
    events = [
        _ev("b", datetime.date(2026, 11, 20)),
        _ev("x", datetime.date(2026, 11, 14), status="Done"),
        _ev("a", datetime.date(2026, 11, 14), start=datetime.time(18, 0)),
        _ev("c", datetime.date(2026, 11, 14), start=datetime.time(21, 0)),
    ]
    assert [e.page_id for e in future_events(events, TODAY)] == ["a", "c", "b"]


def test_event_snapshot_none() -> None:
    assert event_snapshot(None) == "none"


def test_event_snapshot_sensitivity() -> None:
    d = datetime.date(2026, 11, 14)
    base = _ev("e", d, slot_count=2, slots=[make_slot(1), make_slot(2)])
    snap = event_snapshot(base)
    booked = _ev("e", d, slot_count=2, slots=[make_slot(1, dj_page_id="dj"), make_slot(2)])
    assert event_snapshot(booked) != snap
    assert event_snapshot(_ev("e", d, slot_count=2, slots=[make_slot(1)])) != snap
    assert event_snapshot(_ev("e", d, slot_count=3, slots=[make_slot(1), make_slot(2)])) != snap
    assert event_snapshot(
        _ev("e", d, slot_count=2, slots=[make_slot(1), make_slot(2)], start=datetime.time(21, 0))
    ) != snap
    assert event_snapshot(
        _ev("e", d, slot_count=2, slots=[make_slot(1), make_slot(2)], slot_minutes=30)
    ) != snap
    assert event_snapshot(
        _ev("e", d, slot_count=2, slots=[make_slot(1), make_slot(2)], status="Done")
    ) == snap


@pytest.mark.asyncio
async def test_refresh_posts_once_for_changed_snapshot() -> None:
    event = _ev("e", datetime.date(2026, 11, 14), slots=[make_slot(1, dj_page_id="d", dj_name="A")])
    service, cfg, channel, _log, _n = _service()
    with _Ctx(cfg):
        assert await service.refresh([event]) is True
        assert await service.refresh([event]) is False
    channel.send.assert_awaited_once()
    embed = channel.send.call_args.kwargs["embed"]
    assert isinstance(embed, LineupUpdateEmbed)
    assert cfg.values["lineup_post_snapshot"] == event_snapshot(event)


@pytest.mark.asyncio
async def test_refresh_posts_new_upcoming_when_first_becomes_done() -> None:
    first = _ev("a", datetime.date(2026, 11, 14))
    second = _ev("b", datetime.date(2026, 11, 21))
    service, cfg, channel, _log, _n = _service()
    with _Ctx(cfg):
        await service.refresh([first, second])
        first_done = _ev("a", datetime.date(2026, 11, 14), status="Done")
        assert await service.refresh([first_done, second]) is True
    assert channel.send.await_count == 2
    assert channel.send.call_args.kwargs["embed"].footer.text == "b"


@pytest.mark.asyncio
async def test_refresh_without_upcoming_posts_nothing() -> None:
    service, cfg, channel, _log, _n = _service()
    with _Ctx(cfg):
        assert await service.refresh([_ev("p", datetime.date(2026, 1, 1))]) is False
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_loads_events_when_none() -> None:
    event = _ev("e", datetime.date(2026, 11, 14))
    service, cfg, channel, _log, notion = _service(events=[event])
    with _Ctx(cfg):
        assert await service.refresh() is True
    notion.list_events.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_notify_changed_merges_burst_into_one_refresh() -> None:
    service, cfg, _c, _l, _n = _service()
    service.refresh = AsyncMock(return_value=False)  # type: ignore[method-assign]
    with patch("bot.services.lineup.asyncio.sleep", new=AsyncMock()) as sleep:
        for _ in range(5):
            service.notify_changed()
        assert service._task is not None
        await service._task
    service.refresh.assert_awaited_once_with()
    sleep.assert_awaited_once_with(LINEUP_DEBOUNCE_SECONDS)


@pytest.mark.asyncio
async def test_notify_changed_after_completion_schedules_again() -> None:
    service, cfg, _c, _l, _n = _service()
    service.refresh = AsyncMock(return_value=False)  # type: ignore[method-assign]
    with patch("bot.services.lineup.asyncio.sleep", new=AsyncMock()):
        service.notify_changed()
        await service._task
        service.notify_changed()
        await service._task
    assert service.refresh.await_count == 2


@pytest.mark.asyncio
async def test_notify_changed_swallows_refresh_errors() -> None:
    service, cfg, _c, _l, _n = _service()
    service.refresh = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    with patch("bot.services.lineup.asyncio.sleep", new=AsyncMock()):
        service.notify_changed()
        await service._task


@pytest.mark.asyncio
async def test_concurrent_refresh_posts_once() -> None:
    event = _ev("e", datetime.date(2026, 11, 14))
    service, cfg, channel, _log, _n = _service()

    async def slow_send(**_kw: object) -> None:
        await asyncio.sleep(0)

    channel.send = AsyncMock(side_effect=slow_send)
    with _Ctx(cfg):
        results = await asyncio.gather(*(service.refresh([event]) for _ in range(5)))
    assert results.count(True) == 1
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_unset_channel_stores_snapshot_and_posts_admin_error_once() -> None:
    event = _ev("e", datetime.date(2026, 11, 14))
    cfg = FakeConfig()
    cfg.values["dj_channel"] = None
    service, cfg, channel, log, _n = _service(cfg=cfg)
    with _Ctx(cfg):
        assert await service.refresh([event]) is False
        assert await service.refresh([event]) is False
    channel.send.assert_not_awaited()
    assert cfg.values["lineup_post_snapshot"] == event_snapshot(event)
    log.log_attention.assert_awaited_once_with(
        "Something failed",
        "The lineup could not be posted because no DJ channel is set.",
        "Run /config set dj-channel to set the DJ channel.",
    )


@pytest.mark.asyncio
async def test_http_exception_keeps_snapshot_and_retries() -> None:
    event = _ev("e", datetime.date(2026, 11, 14))
    service, cfg, channel, _log, _n = _service()
    channel.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(status=500), "x"))
    with _Ctx(cfg):
        assert await service.refresh([event]) is False
        assert "lineup_post_snapshot" not in cfg.values
        channel.send = AsyncMock()
        assert await service.refresh([event]) is True
    assert cfg.values["lineup_post_snapshot"] == event_snapshot(event)


@pytest.mark.asyncio
async def test_missing_channel_returns_false_without_storing() -> None:
    event = _ev("e", datetime.date(2026, 11, 14))
    service, cfg, _c, _l, _n = _service()
    service.bot.get_channel = MagicMock(return_value=None)
    with _Ctx(cfg):
        assert await service.refresh([event]) is False
    assert "lineup_post_snapshot" not in cfg.values


@pytest.mark.asyncio
async def test_post_event_posts_with_view_and_stores_snapshot_for_upcoming() -> None:
    event = _ev("e", datetime.date(2026, 11, 14))
    later = _ev("l", datetime.date(2026, 11, 21))
    service, cfg, channel, _log, _n = _service(events=[event, later])
    service.view = MagicMock()
    with _Ctx(cfg):
        assert await service.post_event(event) is True
        assert cfg.values["lineup_post_snapshot"] == event_snapshot(event)
        cfg.values.pop("lineup_post_snapshot")
        assert await service.post_event(later) is True
        assert "lineup_post_snapshot" not in cfg.values
    assert channel.send.call_args_list[0].kwargs["view"] is service.view


@pytest.mark.asyncio
async def test_post_event_false_when_channel_unset() -> None:
    cfg = FakeConfig()
    cfg.values["dj_channel"] = None
    service, cfg, channel, _l, _n = _service(cfg=cfg)
    with _Ctx(cfg):
        assert await service.post_event(_ev("e", datetime.date(2026, 11, 14))) is False
    channel.send.assert_not_awaited()


def test_event_snapshot_duplicate_rows_do_not_raise_and_use_lowest_page_id() -> None:
    d = datetime.date(2026, 11, 14)
    a = _ev("e", d, slot_count=1, slots=[
        make_slot(1, dj_page_id="dj", page_id="p2"), make_slot(1, page_id="p1")])
    b = _ev("e", d, slot_count=1, slots=[
        make_slot(1, page_id="p1"), make_slot(1, dj_page_id="dj", page_id="p2")])
    c = _ev("e", d, slot_count=1, slots=[
        make_slot(1, dj_page_id="dj", page_id="p1"), make_slot(1, page_id="p2")])
    assert event_snapshot(a) == event_snapshot(b)
    assert event_snapshot(c) != event_snapshot(a)
