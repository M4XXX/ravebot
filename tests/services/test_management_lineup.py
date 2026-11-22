"""Tests for bot/services/management_lineup.py."""
import datetime
import itertools
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import disnake
import pytest

from bot.db.management_lineup import LineupRecord
from bot.services.management_lineup import (
    MANAGEMENT_CHANNEL_KEY,
    MANAGEMENT_DEBOUNCE_SECONDS,
    ManagementLineupService,
    notify_management,
)
from bot.ui import texts
from bot.ui.management_lineup import build_channel_messages
from tests.fake_events import fake_events
from tests.notion.factories import make_event, make_slot

TODAY = datetime.date(2026, 11, 22)
GUILD = 4242
CHANNEL = 500
OTHER_CHANNEL = 600

_ids = itertools.count(1000)


def _not_found() -> disnake.NotFound:
    return disnake.NotFound(MagicMock(status=404, reason="gone"), "Unknown Message")


def _forbidden() -> disnake.Forbidden:
    return disnake.Forbidden(MagicMock(status=403, reason="x"), "Missing Access")


class FakeMessage:
    def __init__(self, channel: "FakeChannel", kwargs: dict) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.kwargs = kwargs
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        if self.id in self.channel.deleted:
            raise _not_found()
        self.edits.append(kwargs)
        self.channel.edit_calls.append((self.id, kwargs))


class _GoneMessage:
    """Partial message for an id the channel never had (or lost)."""

    def __init__(self, message_id: int) -> None:
        self.id = message_id

    async def edit(self, **kwargs) -> None:
        raise _not_found()


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []
        self.edit_calls: list[tuple[int, dict]] = []
        self.deleted: set[int] = set()
        self.fetch_calls = 0
        self.send_error: Exception | None = None

    async def send(self, **kwargs) -> FakeMessage:
        if self.send_error is not None:
            raise self.send_error
        msg = FakeMessage(self, kwargs)
        self.messages[msg.id] = msg
        self.sent.append(msg)
        return msg

    async def fetch_message(self, message_id: int) -> FakeMessage:
        # A staff channel without Read Message History: fetching is forbidden
        self.fetch_calls += 1
        raise _forbidden()

    def get_partial_message(self, message_id: int):
        return self.messages.get(message_id) or _GoneMessage(message_id)

    @property
    def discord_calls(self) -> int:
        return len(self.sent) + len(self.edit_calls) + self.fetch_calls


class FakeStore:
    """In-memory management_lineup_messages table."""

    def __init__(self) -> None:
        self.records: dict[str, LineupRecord] = {}
        self.deleted: list[str] = []

    def patch(self, monkeypatch) -> None:
        async def get_record(pool, event_id):
            return self.records.get(event_id)

        async def list_records(pool):
            return list(self.records.values())

        async def upsert_record(pool, event_id, channel_id, message_ids, content_hash):
            self.records[event_id] = LineupRecord(
                event_id, int(channel_id), tuple(message_ids), content_hash
            )

        async def delete_record(pool, event_id):
            self.deleted.append(event_id)
            self.records.pop(event_id, None)

        async def get_record_by_message(pool, message_id):
            return next(
                (r for r in self.records.values() if message_id in r.message_ids), None
            )

        base = "bot.services.management_lineup.ml_db"
        monkeypatch.setattr(f"{base}.get_record", get_record)
        monkeypatch.setattr(f"{base}.list_records", list_records)
        monkeypatch.setattr(f"{base}.upsert_record", upsert_record)
        monkeypatch.setattr(f"{base}.delete_record", delete_record)
        monkeypatch.setattr(f"{base}.get_record_by_message", get_record_by_message)


class Env:
    def __init__(self, monkeypatch) -> None:
        self.store = FakeStore()
        self.store.patch(monkeypatch)
        self.config: dict[str, str | None] = {MANAGEMENT_CHANNEL_KEY: str(CHANNEL)}
        self.djs: dict[int, dict] = {}
        self.dj_detail_calls: list[list[int]] = []
        self.active = True
        self.channels = {CHANNEL: FakeChannel(CHANNEL), OTHER_CHANNEL: FakeChannel(OTHER_CHANNEL)}

        async def get_value(pool, guild_id, key):
            assert guild_id == GUILD
            return self.config.get(key)

        async def get_dj_details(pool, user_ids):
            self.dj_detail_calls.append(list(user_ids))
            return {uid: dict(self.djs[uid]) for uid in user_ids if uid in self.djs}

        async def is_active(db, guild_id):
            return self.active

        monkeypatch.setattr("bot.services.management_lineup.get_value", get_value)
        monkeypatch.setattr("bot.services.management_lineup.get_dj_details", get_dj_details)
        monkeypatch.setattr("bot.services.management_lineup.activation.is_active", is_active)
        monkeypatch.setattr("bot.services.management_lineup._today", lambda: TODAY)

        self.bot = MagicMock()
        self.bot.guilds = [MagicMock(id=GUILD)]
        self.bot.get_channel = MagicMock(side_effect=lambda cid: self.channels.get(cid))
        self.bot.is_ready = MagicMock(return_value=True)
        self.bot.fetch_channel = AsyncMock(side_effect=_not_found())
        self.log = MagicMock(log_attention=AsyncMock())
        self.view = MagicMock(spec=disnake.ui.View)
        self.service = ManagementLineupService(self.bot, MagicMock(), self.log, view=self.view)

    @property
    def channel(self) -> FakeChannel:
        return self.channels[CHANNEL]


@pytest.fixture
def env(monkeypatch) -> Env:
    return Env(monkeypatch)


def _event(
    page_id: str = "ev-1",
    booked: int = 3,
    count: int = 3,
    date: datetime.date = datetime.date(2026, 12, 5),
    status: str | None = "Planned",
    **kw,
):
    slots = [
        make_slot(i, dj_page_id=f"dj-{i}", dj_name=f"DJ {i}", discord_id=100 + i)
        if i <= booked
        else make_slot(i)
        for i in range(1, count + 1)
    ]
    return make_event(
        event_id=f"Event {page_id}", page_id=page_id, date=date, status=status,
        slot_count=count, slots=slots, **kw,
    )


def _all_text(kwargs: dict) -> str:
    parts = [kwargs.get("content") or ""]
    for embed in kwargs.get("embeds") or []:
        parts.append(embed.title or "")
        parts.append(embed.description or "")
    return "\n".join(parts)


# --- posting ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fully_booked_event_posts_once_and_stores_ids(env: Env) -> None:
    env.djs = {101: {"dj_name": "Alpha", "stream_url_primary": "https://a.example/live"}}
    event = _event()
    await env.service.refresh([event])
    assert len(env.channel.sent) == 1
    sent = env.channel.sent[0]
    assert sent.kwargs["view"] is env.view
    assert "https://a.example/live" in _all_text(sent.kwargs)
    record = env.store.records["ev-1"]
    assert record.channel_id == CHANNEL
    assert record.message_ids == (sent.id,)
    assert record.content_hash
    assert env.dj_detail_calls == [[101, 102, 103]]


@pytest.mark.asyncio
async def test_second_refresh_with_unchanged_data_makes_no_discord_call(env: Env) -> None:
    event = _event()
    await env.service.refresh([event])
    calls = env.channel.discord_calls
    await env.service.refresh([event])
    assert env.channel.discord_calls == calls
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_open_event_without_record_posts_nothing(env: Env) -> None:
    await env.service.refresh([_event(booked=2)])
    assert env.channel.sent == []
    assert env.store.records == {}


@pytest.mark.asyncio
async def test_refresh_loads_events_when_none_given(env: Env) -> None:
    fake_events.list_events = AsyncMock(return_value=[_event()])
    await env.service.refresh()
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_refresh_does_nothing_while_idle(env: Env) -> None:
    env.active = False
    await env.service.refresh([_event()])
    assert env.channel.sent == []


# --- editing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_link_edits_instead_of_posting(env: Env) -> None:
    env.djs = {101: {"dj_name": "Alpha", "stream_url_primary": "https://old.example"}}
    event = _event()
    await env.service.refresh([event])
    first = env.channel.sent[0]
    old_hash = env.store.records["ev-1"].content_hash
    env.djs[101]["stream_url_primary"] = "https://new.example"
    await env.service.refresh([event])
    assert len(env.channel.sent) == 1
    assert len(first.edits) == 1
    assert "https://new.example" in _all_text(first.edits[0])
    record = env.store.records["ev-1"]
    assert record.message_ids == (first.id,)
    assert record.content_hash != old_hash


@pytest.mark.asyncio
async def test_deleted_message_is_reposted_and_ids_replaced(env: Env) -> None:
    env.djs = {101: {"dj_name": "Alpha"}}
    event = _event()
    await env.service.refresh([event])
    first = env.channel.sent[0]
    env.channel.deleted.add(first.id)
    env.djs[101]["dj_name"] = "Alpha Two"
    await env.service.refresh([event])
    assert len(env.channel.sent) == 2
    new = env.channel.sent[1]
    assert new.kwargs["view"] is env.view
    assert env.store.records["ev-1"].message_ids == (new.id,)
    assert "Alpha Two" in _all_text(new.kwargs)


@pytest.mark.asyncio
async def test_record_survives_opening_up_and_refill(env: Env) -> None:
    await env.service.refresh([_event(booked=3)])
    first = env.channel.sent[0]
    await env.service.refresh([_event(booked=2)])
    assert len(first.edits) == 1
    assert texts.MGMT_LINEUP_OPEN_NOTE in _all_text(first.edits[0])
    assert "ev-1" in env.store.records
    await env.service.refresh([_event(booked=3)])
    assert len(first.edits) == 2
    assert texts.MGMT_LINEUP_OPEN_NOTE not in _all_text(first.edits[1])
    assert len(env.channel.sent) == 1
    assert env.store.records["ev-1"].message_ids == (first.id,)


@pytest.mark.asyncio
async def test_record_is_edited_even_when_channel_unset_later(env: Env) -> None:
    await env.service.refresh([_event()])
    first = env.channel.sent[0]
    env.config[MANAGEMENT_CHANNEL_KEY] = None
    await env.service.refresh([_event(booked=2)])
    assert len(first.edits) == 1


# --- channel configuration and problems -------------------------------------


@pytest.mark.asyncio
async def test_channel_unset_posts_nothing(env: Env) -> None:
    env.config[MANAGEMENT_CHANNEL_KEY] = None
    await env.service.refresh([_event()])
    assert env.channel.sent == []
    assert env.store.records == {}
    env.log.log_attention.assert_not_awaited()


@pytest.mark.asyncio
async def test_forbidden_send_logs_once_and_does_not_raise(env: Env) -> None:
    env.channel.send_error = _forbidden()
    await env.service.refresh([_event()])
    await env.service.refresh([_event(), _event("ev-2")])
    env.log.log_attention.assert_awaited_once()
    assert env.store.records == {}


@pytest.mark.asyncio
async def test_missing_channel_logs_once_and_does_not_raise(env: Env) -> None:
    env.config[MANAGEMENT_CHANNEL_KEY] = "999"
    await env.service.refresh([_event()])
    await env.service.refresh([_event()])
    env.log.log_attention.assert_awaited_once()
    assert env.store.records == {}


@pytest.mark.asyncio
async def test_unexpected_error_is_logged_not_raised(env: Env) -> None:
    env.channel.send_error = RuntimeError("boom")
    await env.service.refresh([_event(), _event("ev-2")])
    assert env.store.records == {}


@pytest.mark.asyncio
async def test_channel_change_keeps_old_record_channel(env: Env) -> None:
    await env.service.refresh([_event("ev-1")])
    first = env.channel.sent[0]
    env.config[MANAGEMENT_CHANNEL_KEY] = str(OTHER_CHANNEL)
    await env.service.refresh([_event("ev-1", booked=2), _event("ev-2")])
    assert len(first.edits) == 1
    other = env.channels[OTHER_CHANNEL]
    assert len(other.sent) == 1
    assert env.store.records["ev-1"].channel_id == CHANNEL
    assert env.store.records["ev-2"].channel_id == OTHER_CHANNEL
    assert len(env.channel.sent) == 1


# --- finished events --------------------------------------------------------


@pytest.mark.asyncio
async def test_canceled_event_with_record_is_edited_once(env: Env) -> None:
    await env.service.refresh([_event()])
    first = env.channel.sent[0]
    canceled = _event(status="Canceled")
    await env.service.refresh([canceled])
    await env.service.refresh([canceled])
    assert len(first.edits) == 1
    assert texts.MGMT_LINEUP_CANCELED in _all_text(first.edits[0])


@pytest.mark.asyncio
async def test_canceled_event_without_record_posts_nothing(env: Env) -> None:
    await env.service.refresh([_event(status="Canceled")])
    assert env.channel.sent == []


@pytest.mark.asyncio
async def test_done_event_untouched(env: Env) -> None:
    await env.service.refresh([_event()])
    first = env.channel.sent[0]
    await env.service.refresh([_event(booked=2, status="Done"), _event("ev-2", status="Done")])
    assert first.edits == []
    assert len(env.channel.sent) == 1
    assert "ev-2" not in env.store.records


# --- backfill and suppressed (empty-id) records ------------------------------


def _three_events() -> list:
    return [
        _event("late", date=datetime.date(2026, 12, 20)),
        _event("early", date=datetime.date(2026, 11, 28)),
        _event("mid", date=datetime.date(2026, 12, 5)),
        _event("open", booked=1),
        _event("past", date=datetime.date(2026, 11, 1)),
        _event("done", status="Done"),
    ]


@pytest.mark.asyncio
async def test_eligible_backfill_events(env: Env) -> None:
    env.store.records["mid"] = LineupRecord("mid", CHANNEL, (1,), "h")
    events = await env.service.eligible_backfill_events(_three_events())
    assert [e.page_id for e in events] == ["early", "late"]


@pytest.mark.asyncio
async def test_eligible_backfill_events_excludes_suppressed(env: Env) -> None:
    env.store.records["mid"] = LineupRecord("mid", CHANNEL, (), "")
    events = await env.service.eligible_backfill_events(_three_events())
    assert [e.page_id for e in events] == ["early", "late"]


@pytest.mark.asyncio
async def test_backfill_all_posts_in_date_order(env: Env) -> None:
    fake_events.list_events = AsyncMock(return_value=_three_events())
    count = await env.service.backfill("all")
    assert count == 3
    titles = [m.kwargs["embeds"][0].title for m in env.channel.sent]
    assert titles == [
        texts.mgmt_title(e)
        for e in sorted(_three_events()[:3], key=lambda e: e.date)
    ]
    assert set(env.store.records) == {"early", "mid", "late"}
    assert all(r.message_ids for r in env.store.records.values())


@pytest.mark.asyncio
async def test_backfill_all_without_channel_posts_nothing(env: Env) -> None:
    env.config[MANAGEMENT_CHANNEL_KEY] = None
    fake_events.list_events = AsyncMock(return_value=_three_events())
    assert await env.service.backfill("all") == 0
    assert env.store.records == {}


@pytest.mark.asyncio
async def test_backfill_new_suppresses_until_reopened_and_refilled(env: Env) -> None:
    events = _three_events()
    fake_events.list_events = AsyncMock(return_value=events)
    assert await env.service.backfill("new") == 3
    assert env.channel.sent == []
    for key in ("early", "mid", "late"):
        record = env.store.records[key]
        assert record.message_ids == ()
        assert record.channel_id == CHANNEL

    # Still fully booked: nothing posted, suppressed records stay.
    await env.service.refresh(events)
    assert env.channel.sent == []
    assert env.channel.discord_calls == 0
    assert set(env.store.records) == {"early", "mid", "late"}
    assert env.store.deleted == []

    # "mid" opens up: its suppressed record is deleted, nothing is posted.
    opened = _event("mid", booked=2, date=datetime.date(2026, 12, 5))
    await env.service.refresh([events[0], events[1], opened])
    assert env.store.deleted == ["mid"]
    assert "mid" not in env.store.records
    assert env.channel.sent == []

    # Refilled: now it posts.
    await env.service.refresh(events[:3])
    assert len(env.channel.sent) == 1
    assert env.channel.sent[0].kwargs["embeds"][0].title == texts.mgmt_title(events[2])
    assert env.store.records["mid"].message_ids == (env.channel.sent[0].id,)
    assert env.store.records["early"].message_ids == ()


@pytest.mark.asyncio
async def test_suppressed_record_survives_channel_unset_and_canceled(env: Env) -> None:
    env.store.records["ev-1"] = LineupRecord("ev-1", CHANNEL, (), "")
    env.config[MANAGEMENT_CHANNEL_KEY] = None
    await env.service.refresh([_event()])
    await env.service.refresh([_event(status="Canceled")])
    assert env.store.records["ev-1"].message_ids == ()
    assert env.channel.discord_calls == 0


@pytest.mark.asyncio
async def test_suppressed_record_deleted_when_open_even_if_channel_unset(env: Env) -> None:
    env.store.records["ev-1"] = LineupRecord("ev-1", CHANNEL, (), "")
    env.config[MANAGEMENT_CHANNEL_KEY] = None
    await env.service.refresh([_event(booked=1)])
    assert "ev-1" not in env.store.records


@pytest.mark.asyncio
async def test_backfill_new_with_channel_id_works_before_the_channel_is_stored(
    env: Env,
) -> None:
    # The setup wizard suppresses before it stores the channel (no poll gap).
    env.config[MANAGEMENT_CHANNEL_KEY] = None
    fake_events.list_events = AsyncMock(return_value=_three_events())
    assert await env.service.backfill("new", channel_id=OTHER_CHANNEL) == 3
    assert {r.channel_id for r in env.store.records.values()} == {OTHER_CHANNEL}
    assert all(r.message_ids == () for r in env.store.records.values())
    assert env.channel.discord_calls == 0


@pytest.mark.asyncio
async def test_backfill_all_with_channel_id_posts_there(env: Env) -> None:
    fake_events.list_events = AsyncMock(return_value=_three_events())
    assert await env.service.backfill("all", channel_id=OTHER_CHANNEL) == 3
    assert env.channel.sent == []
    assert len(env.channels[OTHER_CHANNEL].sent) == 3


@pytest.mark.asyncio
async def test_backfill_new_returns_zero_without_eligible_events(env: Env) -> None:
    fake_events.list_events = AsyncMock(return_value=[_event(booked=1)])
    assert await env.service.backfill("new") == 0
    assert env.store.records == {}


# --- long lineups -----------------------------------------------------------


def _long_djs(count: int) -> dict[int, dict]:
    return {
        100 + i: {
            "dj_name": f"DJ number {i} with a long name",
            "stream_url_primary": f"https://stream.example.com/{'x' * 120}/{i}",
            "stream_url_backup": f"https://backup.example.com/{'y' * 120}/{i}",
        }
        for i in range(1, count + 1)
    }


@pytest.mark.asyncio
async def test_forty_slot_event_posts_several_messages_view_on_first(env: Env) -> None:
    env.djs = _long_djs(40)
    event = _event(booked=40, count=40, slot_minutes=15)
    await env.service.refresh([event])
    sent = env.channel.sent
    assert len(sent) > 1
    assert sent[0].kwargs["view"] is env.view
    assert all("view" not in m.kwargs for m in sent[1:])
    assert env.store.records["ev-1"].message_ids == tuple(m.id for m in sent)


@pytest.mark.asyncio
async def test_long_lineup_one_missing_message_reposts_all(env: Env) -> None:
    env.djs = _long_djs(40)
    event = _event(booked=40, count=40, slot_minutes=15)
    await env.service.refresh([event])
    old = list(env.channel.sent)
    env.channel.deleted.add(old[-1].id)
    env.djs[101]["dj_name"] = "Renamed"
    await env.service.refresh([event])
    new = env.channel.sent[len(old):]
    assert len(new) == len(old)
    assert new[0].kwargs["view"] is env.view
    assert env.store.records["ev-1"].message_ids == tuple(m.id for m in new)
    # The surviving old messages point to the new lineup and lose the button
    for msg in old[:-1]:
        assert msg.edits[-1] == {"content": texts.MGMT_LINEUP_MOVED, "embeds": [], "view": None}


@pytest.mark.asyncio
async def test_lineup_growing_posts_extra_messages_in_stored_channel(env: Env) -> None:
    env.djs = _long_djs(40)
    await env.service.refresh([_event(booked=3, count=3)])
    first = env.channel.sent[0]
    env.config[MANAGEMENT_CHANNEL_KEY] = str(OTHER_CHANNEL)
    await env.service.refresh([_event(booked=40, count=40, slot_minutes=15)])
    assert len(first.edits) == 1
    extra = env.channel.sent[1:]
    assert extra
    assert env.channels[OTHER_CHANNEL].sent == []
    assert env.store.records["ev-1"].message_ids == (first.id, *[m.id for m in extra])


@pytest.mark.asyncio
async def test_lineup_shrinking_edits_surplus_to_placeholder_and_keeps_ids(env: Env) -> None:
    env.djs = _long_djs(40)
    await env.service.refresh([_event(booked=40, count=40, slot_minutes=15)])
    sent = list(env.channel.sent)
    ids = tuple(m.id for m in sent)
    await env.service.refresh([_event(booked=3, count=3)])
    assert len(env.channel.sent) == len(sent)
    for msg in sent[1:]:
        assert msg.edits[-1] == {"content": texts.MGMT_LINEUP_SURPLUS, "embeds": []}
    assert env.store.records["ev-1"].message_ids == ids
    # Growing again reuses the stored surplus messages.
    await env.service.refresh([_event(booked=40, count=40, slot_minutes=15)])
    assert len(env.channel.sent) == len(sent)
    assert env.store.records["ev-1"].message_ids == ids
    assert sent[1].edits[-1]["content"] is None


@pytest.mark.asyncio
async def test_partial_post_failure_keeps_sent_ids_and_retries(env: Env) -> None:
    env.djs = _long_djs(40)
    event = _event(booked=40, count=40, slot_minutes=15)
    original_send = env.channel.send
    calls = {"n": 0}

    async def flaky_send(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise disnake.HTTPException(MagicMock(status=500, reason="x"), "server")
        return await original_send(**kwargs)

    env.channel.send = flaky_send  # type: ignore[method-assign]
    await env.service.refresh([event])
    record = env.store.records["ev-1"]
    assert len(record.message_ids) == 1
    assert record.content_hash == ""
    await env.service.refresh([event])
    record = env.store.records["ev-1"]
    assert len(record.message_ids) > 1
    assert record.message_ids[0] == env.channel.sent[0].id


# --- debounce and notify_management ----------------------------------------


def test_constants() -> None:
    assert MANAGEMENT_DEBOUNCE_SECONDS == 10
    assert MANAGEMENT_CHANNEL_KEY == "management_channel"
    assert "—" not in texts.MGMT_LINEUP_SURPLUS


@pytest.mark.asyncio
async def test_notify_changed_merges_burst(env: Env) -> None:
    env.service.refresh = AsyncMock()  # type: ignore[method-assign]
    with patch("bot.services.management_lineup.asyncio.sleep", new=AsyncMock()) as sleep:
        for _ in range(4):
            env.service.notify_changed()
        await env.service._task
    env.service.refresh.assert_awaited_once_with()
    sleep.assert_awaited_once_with(MANAGEMENT_DEBOUNCE_SECONDS)


@pytest.mark.asyncio
async def test_notify_changed_idle_does_nothing(env: Env) -> None:
    env.active = False
    env.service.refresh = AsyncMock()  # type: ignore[method-assign]
    with patch("bot.services.management_lineup.asyncio.sleep", new=AsyncMock()):
        env.service.notify_changed()
        await env.service._task
    env.service.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_changed_swallows_errors(env: Env) -> None:
    env.service.refresh = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    with patch("bot.services.management_lineup.asyncio.sleep", new=AsyncMock()):
        env.service.notify_changed()
        await env.service._task
        env.service.notify_changed()
        await env.service._task
    assert env.service.refresh.await_count == 2


def test_notify_management_without_service() -> None:
    notify_management(MagicMock(management_lineup_service=None))


def test_notify_management_calls_service() -> None:
    ctx = MagicMock()
    notify_management(ctx)
    ctx.management_lineup_service.notify_changed.assert_called_once_with()


def test_notify_management_swallows_errors() -> None:
    ctx = MagicMock()
    ctx.management_lineup_service.notify_changed.side_effect = RuntimeError("no loop")
    notify_management(ctx)


def test_context_has_field() -> None:
    from bot.context import BotContext

    assert BotContext(config=MagicMock()).management_lineup_service is None


def _fail_on_call(channel: FakeChannel, n: int, exc: Exception) -> None:
    original_send = channel.send
    calls = {"n": 0}

    async def send(**kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            raise exc
        return await original_send(**kwargs)

    channel.send = send  # type: ignore[method-assign]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc", [TimeoutError(), aiohttp.ClientError("disconnected")]
)
async def test_network_error_mid_post_keeps_sent_ids_and_completes_by_edit(
    env: Env, exc: Exception
) -> None:
    env.djs = _long_djs(40)
    event = _event(booked=40, count=40, slot_minutes=15)
    _fail_on_call(env.channel, 2, exc)
    await env.service.refresh([event])
    first = env.channel.sent[0]
    record = env.store.records["ev-1"]
    assert record.message_ids == (first.id,)
    assert record.content_hash == ""
    await env.service.refresh([event])
    record = env.store.records["ev-1"]
    assert len(first.edits) == 1  # completed by editing, not a second lineup
    assert record.message_ids == tuple(m.id for m in env.channel.sent)
    assert record.content_hash
    assert sum(1 for m in env.channel.sent if "view" in m.kwargs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc", [TimeoutError(), aiohttp.ClientError("disconnected")]
)
async def test_network_error_mid_edit_keeps_extra_ids(env: Env, exc: Exception) -> None:
    env.djs = _long_djs(40)
    await env.service.refresh([_event(booked=3, count=3)])
    first = env.channel.sent[0]
    assert len(build_channel_messages(
        _event(booked=40, count=40, slot_minutes=15), env.djs, "booked"
    )) >= 3
    long_event = _event(booked=40, count=40, slot_minutes=15)
    # Counting from here: send 1 is the first extra chunk, send 2 fails.
    _fail_on_call(env.channel, 2, exc)
    await env.service.refresh([long_event])
    extra = env.channel.sent[1]
    record = env.store.records["ev-1"]
    assert record.message_ids == (first.id, extra.id)
    assert record.content_hash == ""
    sent_before = len(env.channel.sent)
    await env.service.refresh([long_event])
    record = env.store.records["ev-1"]
    assert record.message_ids[:2] == (first.id, extra.id)
    assert record.message_ids == tuple(m.id for m in env.channel.sent)
    assert len(record.message_ids) == len(env.channel.sent)
    assert len(env.channel.sent) > sent_before
    assert record.content_hash


# --- final review fixes -------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_works_without_read_message_history(env: Env) -> None:
    await env.service.refresh([_event()])
    first = env.channel.sent[0]
    await env.service.refresh([_event(booked=2)])
    assert env.channel.fetch_calls == 0
    assert len(first.edits) == 1
    assert len(env.channel.sent) == 1
    env.log.log_attention.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_first_message_turns_survivors_into_placeholders(env: Env) -> None:
    env.djs = _long_djs(40)
    event = _event(booked=40, count=40, slot_minutes=15)
    await env.service.refresh([event])
    old = list(env.channel.sent)
    env.channel.deleted.add(old[0].id)
    env.djs[101]["dj_name"] = "Renamed"
    await env.service.refresh([event])
    new = env.channel.sent[len(old):]
    assert env.store.records["ev-1"].message_ids == tuple(m.id for m in new)
    for msg in old[1:]:
        assert msg.edits[-1] == {"content": texts.MGMT_LINEUP_MOVED, "embeds": [], "view": None}
    assert "—" not in texts.MGMT_LINEUP_MOVED


@pytest.mark.asyncio
async def test_extra_chunk_sent_before_not_found_is_not_orphaned(env: Env) -> None:
    env.djs = _long_djs(40)
    await env.service.refresh([_event(booked=3, count=3)])
    first = env.channel.sent[0]
    # The first extra chunk is sent, the second fails as if the channel vanished.
    _fail_on_call(env.channel, 2, _not_found())
    await env.service.refresh([_event(booked=40, count=40, slot_minutes=15)])
    extra = env.channel.sent[1]
    new = env.channel.sent[2:]
    assert new and new[0].kwargs["view"] is env.view
    assert env.store.records["ev-1"].message_ids == tuple(m.id for m in new)
    for msg in (first, extra):
        assert msg.edits[-1] == {"content": texts.MGMT_LINEUP_MOVED, "embeds": [], "view": None}


@pytest.mark.asyncio
async def test_extra_chunk_ids_kept_when_repost_impossible(env: Env) -> None:
    env.djs = _long_djs(40)
    await env.service.refresh([_event(booked=3, count=3)])
    first = env.channel.sent[0]
    env.config[MANAGEMENT_CHANNEL_KEY] = None
    _fail_on_call(env.channel, 2, _not_found())
    await env.service.refresh([_event(booked=40, count=40, slot_minutes=15)])
    extra = env.channel.sent[1]
    record = env.store.records["ev-1"]
    assert record.message_ids == (first.id, extra.id)
    assert record.content_hash == ""


@pytest.mark.asyncio
async def test_raw_delete_clears_hash_and_notifies(env: Env) -> None:
    await env.service.refresh([_event()])
    first = env.channel.sent[0]
    env.service.notify_changed = MagicMock()  # type: ignore[method-assign]
    await env.service.on_raw_message_delete(MagicMock(message_id=first.id))
    record = env.store.records["ev-1"]
    assert record.content_hash == ""
    assert record.message_ids == (first.id,)
    assert record.channel_id == CHANNEL
    env.service.notify_changed.assert_called_once_with()
    # With unchanged content the next refresh posts the lineup again
    env.channel.deleted.add(first.id)
    await env.service.refresh([_event()])
    assert len(env.channel.sent) == 2
    assert env.store.records["ev-1"].message_ids == (env.channel.sent[1].id,)


@pytest.mark.asyncio
async def test_raw_delete_of_untracked_message_is_ignored(env: Env) -> None:
    await env.service.refresh([_event()])
    before = dict(env.store.records)
    env.service.notify_changed = MagicMock()  # type: ignore[method-assign]
    await env.service.on_raw_message_delete(MagicMock(message_id=123))
    assert env.store.records == before
    env.service.notify_changed.assert_not_called()


@pytest.mark.asyncio
async def test_raw_bulk_delete_clears_each_tracked_record(env: Env) -> None:
    await env.service.refresh([_event("ev-1"), _event("ev-2")])
    a, b = env.channel.sent
    env.store.records["ev-3"] = LineupRecord("ev-3", CHANNEL, (77,), "keep")
    env.service.notify_changed = MagicMock()  # type: ignore[method-assign]
    await env.service.on_raw_bulk_message_delete(MagicMock(message_ids={a.id, b.id, 999}))
    assert env.store.records["ev-1"].content_hash == ""
    assert env.store.records["ev-2"].content_hash == ""
    assert env.store.records["ev-3"].content_hash == "keep"
    env.service.notify_changed.assert_called_once_with()


@pytest.mark.asyncio
async def test_raw_bulk_delete_of_untracked_messages_is_ignored(env: Env) -> None:
    env.service.notify_changed = MagicMock()  # type: ignore[method-assign]
    await env.service.on_raw_bulk_message_delete(MagicMock(message_ids={1, 2}))
    env.service.notify_changed.assert_not_called()


@pytest.mark.asyncio
async def test_raw_delete_never_raises(env: Env, monkeypatch) -> None:
    async def boom(pool, message_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("bot.services.management_lineup.ml_db.get_record_by_message", boom)
    await env.service.on_raw_message_delete(MagicMock(message_id=1))
    await env.service.on_raw_bulk_message_delete(MagicMock(message_ids={1}))


@pytest.mark.asyncio
async def test_lost_channel_reposts_into_configured_channel(env: Env) -> None:
    await env.service.refresh([_event()])
    old_id = env.channel.sent[0].id
    del env.channels[CHANNEL]  # the staff channel was deleted
    env.config[MANAGEMENT_CHANNEL_KEY] = str(OTHER_CHANNEL)
    await env.service.refresh([_event()])  # unchanged content
    other = env.channels[OTHER_CHANNEL]
    assert len(other.sent) == 1
    assert other.sent[0].kwargs["view"] is env.view
    record = env.store.records["ev-1"]
    assert record.channel_id == OTHER_CHANNEL
    assert record.message_ids == (other.sent[0].id,)
    assert old_id not in record.message_ids
    env.log.log_attention.assert_awaited_once()
    await env.service.refresh([_event()])
    assert len(other.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, str(CHANNEL)])
async def test_lost_channel_without_other_configured_channel_only_reports(
    env: Env, configured: str | None
) -> None:
    await env.service.refresh([_event()])
    record = env.store.records["ev-1"]
    del env.channels[CHANNEL]
    env.config[MANAGEMENT_CHANNEL_KEY] = configured
    await env.service.refresh([_event(booked=2)])
    await env.service.refresh([_event(booked=2)])
    assert env.channels[OTHER_CHANNEL].sent == []
    assert env.store.records["ev-1"] == record
    env.log.log_attention.assert_awaited_once()


# --- channel cache gap ----------------------------------------------------


async def _lost_channel_env(env: Env) -> FakeChannel:
    """A posted lineup whose channel is no longer in the cache; another channel is configured."""
    await env.service.refresh([_event()])
    lost = env.channels.pop(CHANNEL)
    env.config[MANAGEMENT_CHANNEL_KEY] = str(OTHER_CHANNEL)
    return lost


@pytest.mark.asyncio
async def test_channel_missing_from_cache_before_ready_is_not_treated_as_deleted(env: Env) -> None:
    await _lost_channel_env(env)
    record = env.store.records["ev-1"]
    env.bot.is_ready.return_value = False
    await env.service.refresh([_event()])
    await env.service.refresh([_event(booked=2)])  # changed content as well
    assert env.channels[OTHER_CHANNEL].sent == []
    assert env.store.records["ev-1"] == record
    env.log.log_attention.assert_not_awaited()
    env.bot.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_miss_but_channel_exists_edits_in_place(env: Env) -> None:
    lost = await _lost_channel_env(env)
    env.bot.fetch_channel = AsyncMock(return_value=lost)  # fetched from the API
    await env.service.refresh([_event(booked=2)])
    assert env.channels[OTHER_CHANNEL].sent == []
    assert len(lost.sent) == 1 and len(lost.sent[0].edits) == 1
    assert env.store.records["ev-1"].channel_id == CHANNEL
    env.log.log_attention.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        disnake.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access"),
        disnake.HTTPException(MagicMock(status=500, reason="Server Error"), "boom"),
        TimeoutError(),
    ],
)
async def test_unknown_channel_state_changes_nothing(env: Env, error: Exception) -> None:
    await _lost_channel_env(env)
    record = env.store.records["ev-1"]
    env.bot.fetch_channel = AsyncMock(side_effect=error)
    await env.service.refresh([_event()])
    await env.service.refresh([_event(booked=2)])
    assert env.channels[OTHER_CHANNEL].sent == []
    assert env.store.records["ev-1"] == record
    env.log.log_attention.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_post_before_ready_waits_without_a_false_report(env: Env) -> None:
    env.bot.is_ready.return_value = False
    del env.channels[CHANNEL]  # not cached yet
    await env.service.refresh([_event()])
    assert env.store.records == {}
    env.log.log_attention.assert_not_awaited()
