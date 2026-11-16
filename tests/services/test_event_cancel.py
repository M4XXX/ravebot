"""Tests for bot/services/event_cancel.py (EventCancelService behind /cancel event).

Notion, Postgres and Discord are mocked.
"""
import dataclasses
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.db.bookings import SlotLock
from bot.notion.client import NotionWriteError
from bot.services.event_cancel import EventCancelService
from bot.ui import texts
from bot.ui.embeds import ErrorCode
from tests.notion.factories import make_event, make_slot

PAGE = "evt-page"
EVENT_ID = "2026-11-28 Rave Night"
TODAY = datetime.date(2026, 11, 20)
ALPHA, BETA, GUEST = 101, 202, -5
MOD = "bot.services.event_cancel"


def _event(*slots, status: str | None = "Planning stage", date=datetime.date(2026, 11, 28)):
    return make_event(
        event_id=EVENT_ID,
        page_id=PAGE,
        date=date,
        status=status,
        start=datetime.time(22, 0),
        slot_minutes=30,
        slot_count=4,
        slots=slots or [make_slot(n) for n in range(1, 5)],
    )


def _row(number: int, discord_id: int | None, name: str, page: str | None = None, extras=()):
    return make_slot(
        number,
        dj_page_id=page or f"dj-page-{name}",
        dj_name=name,
        discord_id=discord_id,
        extra_dj_page_ids=extras,
    )


def _lock(holder: int | None) -> SlotLock:
    return SlotLock(discord_user_id=holder, booked_at=None)


class Env:
    def __init__(self, monkeypatch, event=None, locks=None, names=None, dj_channel="555"):
        self.event = event if event is not None else _event()
        self.users: dict[int, MagicMock] = {}
        self.bot = MagicMock()
        self.bot.guilds = [MagicMock(id=1)]
        self.bot.get_user = MagicMock(side_effect=self._user)
        self.bot.fetch_user = AsyncMock(side_effect=self._user)
        self.channel = MagicMock()
        self.channel.send = AsyncMock()
        self.bot.get_channel = MagicMock(return_value=self.channel)

        self.notion = MagicMock()
        self.notion.list_events = AsyncMock(return_value=[self.event])
        self.notion.set_event_status = AsyncMock()
        self.notion.empty_slot = AsyncMock()

        self.log = MagicMock()
        for name in ("post_error", "log_attention", "log_event_canceled"):
            setattr(self.log, name, AsyncMock())
        self.lineup = MagicMock()
        self.lineup.notify_changed = MagicMock()

        self.db = MagicMock(name="pool")
        self.ctx = SimpleNamespace(
            db=self.db, notion_client=self.notion, log_service=self.log,
            lineup_service=self.lineup,
        )

        self.locks = {(PAGE, i): _lock(h) for i, h in (locks or {}).items()}
        names = names or {}
        self.get_locks = AsyncMock(side_effect=lambda pool, ids: dict(self.locks))
        self.release = AsyncMock(return_value=True)

        async def get_dj_row(pool, user_id):
            return {"dj_name": names[user_id]} if user_id in names else None

        monkeypatch.setattr(f"{MOD}.get_slot_locks_for_events", self.get_locks)
        monkeypatch.setattr(f"{MOD}.release_slot_lock", self.release)
        monkeypatch.setattr(f"{MOD}.get_dj_row", get_dj_row)
        monkeypatch.setattr(f"{MOD}.get_value", AsyncMock(return_value=dj_channel))
        monkeypatch.setattr(f"{MOD}._today", lambda: TODAY)
        self.svc = EventCancelService(self.ctx, self.bot)  # type: ignore[arg-type]

    def _user(self, user_id: int) -> MagicMock:
        if user_id not in self.users:
            user = MagicMock()
            user.send = AsyncMock()
            self.users[user_id] = user
        return self.users[user_id]

    def dm_texts(self, user_id: int) -> list[str]:
        return [c.args[0] for c in self._user(user_id).send.await_args_list]

    def released(self) -> list[int]:
        return sorted(c.args[2] for c in self.release.await_args_list)

    def emptied(self) -> list[int]:
        return sorted(c.args[1] for c in self.notion.empty_slot.await_args_list)

    def nothing_sent(self) -> None:
        for user in self.users.values():
            user.send.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.log.log_event_canceled.assert_not_awaited()
        self.log.post_error.assert_not_awaited()


def _two_djs(monkeypatch, **kw) -> Env:
    event = _event(
        _row(1, ALPHA, "DJ Alpha"), make_slot(2), _row(3, BETA, "DJ Beta"), make_slot(4)
    )
    return Env(monkeypatch, event, {0: ALPHA, 1: None, 2: BETA, 3: None}, **kw)


@pytest.mark.asyncio
async def test_cancel_without_bookings_logs_only(monkeypatch):
    env = Env(monkeypatch, locks={0: None, 1: None, 2: None, 3: None})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    assert result.message == f"Canceled {EVENT_ID}."
    env.notion.set_event_status.assert_awaited_once_with(env.event, "Canceled")
    env.log.log_event_canceled.assert_awaited_once_with("Max", env.event, [])
    env.channel.send.assert_not_awaited()
    env.notion.empty_slot.assert_not_awaited()
    env.release.assert_not_awaited()
    env.lineup.notify_changed.assert_called_once_with()


@pytest.mark.asyncio
async def test_cancel_with_two_djs_dms_logs_and_posts_in_the_dj_channel(monkeypatch):
    env = _two_djs(monkeypatch)
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    assert result.notified == ("DJ Alpha", "DJ Beta")
    assert result.by_hand == ()
    assert env.dm_texts(ALPHA) == [texts.dm_event_canceled(env.event, [0])]
    assert env.dm_texts(BETA) == [texts.dm_event_canceled(env.event, [2])]
    assert env.emptied() == [0, 2]
    assert env.released() == [0, 2]
    env.log.log_event_canceled.assert_awaited_once_with(
        "Max",
        env.event,
        [texts.dj_line("DJ Alpha", env.event, [0]), texts.dj_line("DJ Beta", env.event, [2])],
    )
    env.channel.send.assert_awaited_once()
    assert env.channel.send.await_args.args[0] == texts.dj_channel_event_canceled(env.event)
    mentions = env.channel.send.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False
    env.bot.get_channel.assert_called_once_with(555)
    env.log.post_error.assert_not_awaited()
    assert result.message == texts.event_cancel_done(env.event, ["a", "b"], [], [])


@pytest.mark.asyncio
async def test_event_name_matches_case_insensitively(monkeypatch):
    env = _two_djs(monkeypatch)
    result = await env.svc.cancel_event("Max", EVENT_ID.upper())
    assert result.ok is True


@pytest.mark.asyncio
async def test_dj_with_two_slots_gets_one_dm(monkeypatch):
    event = _event(
        _row(1, ALPHA, "DJ Alpha"), make_slot(2), _row(3, ALPHA, "DJ Alpha"), make_slot(4)
    )
    env = Env(monkeypatch, event, {0: ALPHA, 2: ALPHA})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert env.dm_texts(ALPHA) == [texts.dm_event_canceled(event, [0, 2])]
    assert result.notified == ("DJ Alpha",)
    assert env.released() == [0, 2]


@pytest.mark.asyncio
async def test_blocked_dm_is_reported(monkeypatch):
    env = _two_djs(monkeypatch)
    env._user(ALPHA).send.side_effect = disnake.Forbidden(MagicMock(status=403), "closed")
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    assert result.notified == ("DJ Beta",)
    assert result.by_hand == ("DJ Alpha",)
    env.log.post_error.assert_awaited_once_with(
        ErrorCode.ERR_DM_FAILED,
        texts.event_canceled_dm_failed("DJ Alpha", env.event),
        texts.EVENT_CANCELED_ANYWAY,
        texts.dm_failed_todo("DJ Alpha"),
    )
    env.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_gets_a_by_hand_entry_and_no_dm_attempt(monkeypatch):
    event = _event(_row(1, GUEST, "Guesty"), make_slot(2), make_slot(3), make_slot(4))
    env = Env(monkeypatch, event, {0: GUEST})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    assert result.by_hand == ("Guesty",)
    assert result.notified == ()
    env.bot.get_user.assert_not_called()
    env.bot.fetch_user.assert_not_awaited()
    env.log.post_error.assert_awaited_once_with(
        ErrorCode.ERR_DM_FAILED,
        texts.event_canceled_no_discord("Guesty", event),
        texts.EVENT_CANCELED_ANYWAY,
        texts.tell_canceled_todo("Guesty"),
    )
    # a guest is a booked DJ: the DJ channel is told
    env.channel.send.assert_awaited_once()
    assert env.released() == [0]
    assert env.emptied() == [0]


@pytest.mark.asyncio
async def test_notion_dj_without_lock_and_without_discord_id_is_by_hand(monkeypatch):
    event = _event(make_slot(1), _row(2, None, "Handmade"), make_slot(3), make_slot(4))
    env = Env(monkeypatch, event, {})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.by_hand == ("Handmade",)
    env.bot.get_user.assert_not_called()
    assert env.emptied() == [1]
    env.release.assert_not_awaited()
    env.log.post_error.assert_awaited_once()
    assert env.log.post_error.await_args.args[3] == texts.tell_canceled_todo("Handmade")


@pytest.mark.asyncio
async def test_notion_dj_without_lock_is_notified_and_slot_emptied(monkeypatch):
    event = _event(make_slot(1), _row(2, BETA, "DJ Beta"), make_slot(3), make_slot(4))
    env = Env(monkeypatch, event, {0: None, 1: None})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.notified == ("DJ Beta",)
    assert env.dm_texts(BETA) == [texts.dm_event_canceled(event, [1])]
    assert env.emptied() == [1]


@pytest.mark.asyncio
async def test_lock_without_notion_dj_is_released_and_notified(monkeypatch):
    env = Env(monkeypatch, locks={3: ALPHA}, names={ALPHA: "DJ Alpha"})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.notified == ("DJ Alpha",)
    assert env.dm_texts(ALPHA) == [texts.dm_event_canceled(env.event, [3])]
    assert env.released() == [3]
    env.notion.empty_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_holder_without_any_name_falls_back_to_the_id(monkeypatch):
    env = Env(monkeypatch, locks={0: ALPHA})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.notified == (str(ALPHA),)


@pytest.mark.asyncio
async def test_extra_djs_of_a_slot_row_are_emptied_too(monkeypatch):
    event = _event(
        make_slot(1, extra_dj_page_ids=("stray",)), make_slot(2), make_slot(3), make_slot(4)
    )
    env = Env(monkeypatch, event, {})
    await env.svc.cancel_event("Max", EVENT_ID)
    assert env.emptied() == [0]


@pytest.mark.asyncio
async def test_status_write_failure_changes_nothing(monkeypatch):
    env = _two_djs(monkeypatch)
    env.notion.set_event_status.side_effect = NotionWriteError("boom")
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is False
    assert result.message == texts.EVENT_CANCEL_NOTION_FAILED
    env.notion.empty_slot.assert_not_awaited()
    env.release.assert_not_awaited()
    env.nothing_sent()
    env.lineup.notify_changed.assert_not_called()


@pytest.mark.asyncio
async def test_slot_cleanup_failure_continues(monkeypatch):
    env = _two_djs(monkeypatch)

    async def empty_slot(event, index):
        if index == 0:
            raise NotionWriteError("boom")

    env.notion.empty_slot.side_effect = empty_slot
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    assert result.failed_slots == (0,)
    assert env.emptied() == [0, 2]
    assert env.released() == [0, 2]
    assert result.notified == ("DJ Alpha", "DJ Beta")
    env.log.log_attention.assert_awaited_once_with(
        texts.LOG_FAILED_TITLE,
        texts.event_cancel_cleanup_failed(env.event, [0]),
        texts.EVENT_CANCEL_CLEANUP_TODO,
    )
    assert "see the log channel" in result.message


@pytest.mark.asyncio
async def test_lock_release_failure_does_not_stop_the_run(monkeypatch):
    env = _two_djs(monkeypatch)
    env.release.side_effect = RuntimeError("db down")
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    assert result.notified == ("DJ Alpha", "DJ Beta")
    env.channel.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dj_channel", [None, "555"])
async def test_missing_dj_channel_is_reported_and_cancel_still_succeeds(monkeypatch, dj_channel):
    env = _two_djs(monkeypatch, dj_channel=dj_channel)
    env.bot.get_channel = MagicMock(return_value=None)  # "555" set, channel deleted
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    env.log.log_attention.assert_awaited_once_with(
        texts.LOG_FAILED_TITLE, texts.EVENT_CANCEL_POST_FAILED, texts.DJ_CHANNEL_TODO
    )
    assert result.notified == ("DJ Alpha", "DJ Beta")


@pytest.mark.asyncio
async def test_failed_dj_channel_post_is_reported(monkeypatch):
    env = _two_djs(monkeypatch)
    env.channel.send.side_effect = disnake.HTTPException(MagicMock(status=500), "x")
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    env.log.log_attention.assert_awaited_once_with(
        texts.LOG_FAILED_TITLE, texts.EVENT_CANCEL_POST_FAILED, texts.DJ_CHANNEL_TODO
    )
    env.lineup.notify_changed.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _event(status="Canceled"),
        _event(status="Done"),
        _event(date=datetime.date(2026, 11, 19)),  # yesterday
        dataclasses.replace(_event(), event_id="Some other event"),
    ],
    ids=["canceled", "done", "past", "unknown"],
)
async def test_unknown_past_done_and_canceled_events_are_refused(monkeypatch, event):
    env = Env(monkeypatch, event, {0: ALPHA})
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is False
    assert result.message == texts.EVENT_NOT_CANCELABLE
    env.notion.set_event_status.assert_not_awaited()
    env.release.assert_not_awaited()
    env.nothing_sent()
    assert await env.svc.preview(EVENT_ID) is None


@pytest.mark.asyncio
async def test_an_event_dated_today_can_be_canceled(monkeypatch):
    env = Env(monkeypatch, _event(date=TODAY), {})
    assert (await env.svc.cancel_event("Max", EVENT_ID)).ok is True


@pytest.mark.asyncio
async def test_second_cancel_of_the_same_event_sends_nothing(monkeypatch):
    env = _two_djs(monkeypatch)

    async def mark_canceled(event, status):
        env.notion.list_events.return_value = [dataclasses.replace(env.event, status=status)]

    env.notion.set_event_status.side_effect = mark_canceled
    first = await env.svc.cancel_event("Max", EVENT_ID)
    second = await env.svc.cancel_event("Max", EVENT_ID)
    assert first.ok is True
    assert second.ok is False
    assert second.message == texts.EVENT_NOT_CANCELABLE
    assert len(env.dm_texts(ALPHA)) == 1
    env.channel.send.assert_awaited_once()
    env.log.log_event_canceled.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_cancels_at_once_notify_once(monkeypatch):
    import asyncio

    env = _two_djs(monkeypatch)

    async def mark_canceled(event, status):
        await asyncio.sleep(0)
        env.notion.list_events.return_value = [dataclasses.replace(env.event, status=status)]

    env.notion.set_event_status.side_effect = mark_canceled
    results = await asyncio.gather(
        env.svc.cancel_event("Max", EVENT_ID), env.svc.cancel_event("Mia", EVENT_ID)
    )
    assert sorted(r.ok for r in results) == [False, True]
    assert len(env.dm_texts(ALPHA)) == 1
    env.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_reads_bookings_at_cancel_time(monkeypatch):
    event = _event(_row(1, ALPHA, "DJ Alpha"), make_slot(2), make_slot(3), make_slot(4))
    env = Env(monkeypatch, event, {0: ALPHA}, names={BETA: "DJ Beta"})
    preview = await env.svc.preview(EVENT_ID)
    assert preview is not None
    assert [d.name for d in preview.djs] == ["DJ Alpha"]
    env.locks[(PAGE, 2)] = _lock(BETA)  # booked after the preview
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.notified == ("DJ Alpha", "DJ Beta")
    assert env.released() == [0, 2]


@pytest.mark.asyncio
async def test_preview_lists_booked_djs_and_changes_nothing(monkeypatch):
    event = _event(
        _row(1, ALPHA, "DJ Alpha"), _row(2, GUEST, "Guesty"), _row(3, ALPHA, "DJ Alpha"),
        make_slot(4),
    )
    env = Env(monkeypatch, event, {0: ALPHA, 1: GUEST, 2: ALPHA})
    preview = await env.svc.preview(EVENT_ID.lower())
    assert preview is not None
    assert preview.event is event
    alpha, guest = preview.djs
    assert (alpha.dj_id, alpha.name, alpha.slot_indexes, alpha.reachable) == (
        ALPHA, "DJ Alpha", (0, 2), True
    )
    assert (guest.dj_id, guest.name, guest.slot_indexes, guest.reachable) == (
        GUEST, "Guesty", (1,), False
    )
    env.notion.set_event_status.assert_not_awaited()
    env.notion.empty_slot.assert_not_awaited()
    env.release.assert_not_awaited()
    env.nothing_sent()


@pytest.mark.asyncio
async def test_preview_returns_none_for_an_unknown_event(monkeypatch):
    env = Env(monkeypatch)
    assert await env.svc.preview("nope") is None


@pytest.mark.asyncio
async def test_notion_read_failure_gives_a_generic_error(monkeypatch):
    env = Env(monkeypatch)
    env.notion.list_events.side_effect = RuntimeError("down")
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert (result.ok, result.message) == (False, texts.GENERIC_ERROR)
    assert await env.svc.preview(EVENT_ID) is None
    env.notion.set_event_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_without_notion_or_db_is_not_ready(monkeypatch):
    env = Env(monkeypatch)
    env.ctx.notion_client = None
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert (result.ok, result.message) == (False, texts.GENERIC_ERROR)
    assert await env.svc.preview(EVENT_ID) is None


@pytest.mark.asyncio
async def test_unreadable_bookings_stop_the_cancel_before_anything_changes(monkeypatch):
    env = _two_djs(monkeypatch)
    env.get_locks.side_effect = RuntimeError("db down")
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert (result.ok, result.message) == (False, texts.GENERIC_ERROR)
    env.notion.set_event_status.assert_not_awaited()  # bookings are read before the write
    env.nothing_sent()


@pytest.mark.asyncio
async def test_cancel_works_without_a_log_service(monkeypatch):
    env = _two_djs(monkeypatch)
    env.ctx.log_service = None
    env.ctx.lineup_service = None
    result = await env.svc.cancel_event("Max", EVENT_ID)
    assert result.ok is True
    assert result.notified == ("DJ Alpha", "DJ Beta")
