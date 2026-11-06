"""Tests for NotionSyncService.run_cycle (applies plan_sync actions)."""
import datetime
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.bookings import SlotLock
from bot.notion.client import NotionWriteError, SlotRowMissingError
from bot.services.notion_sync import NotionSyncService
from bot.ui.embeds import ErrorCode
from tests.notion.factories import make_event, make_slot

X, Y = 111, 222
PAGE = "event-page"
OLD = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def lock(holder: int | None) -> SlotLock:
    return SlotLock(discord_user_id=holder, booked_at=OLD)


def event_with(*slots, slot_count: int = 2, page_id: str = PAGE, event_id: str = "Rave"):
    return make_event(event_id=event_id, page_id=page_id, slot_count=slot_count, slots=slots)


def dj_row(number: int, discord_id: int | None, page: str = "dj-page", extras=()):
    return make_slot(
        number, dj_page_id=page, dj_name=page, discord_id=discord_id, extra_dj_page_ids=extras
    )


class Env:
    def __init__(self, monkeypatch, events, locks=None, *, log=True):
        self.user = MagicMock()
        self.user.send = AsyncMock()
        self.user.__str__ = MagicMock(return_value="holder-name")
        self.bot = MagicMock()
        self.bot.get_user = MagicMock(return_value=self.user)
        self.bot.fetch_user = AsyncMock(return_value=self.user)
        self.db = MagicMock(name="pool")
        self.notion = MagicMock()
        self.notion.list_events = AsyncMock(return_value=events)
        for name in (
            "create_slot_rows", "trash_slot_rows", "force_assign_slot", "keep_first_dj",
            "assign_slot", "clear_slot", "upsert_dj",
        ):
            setattr(self.notion, name, AsyncMock())
        self.log = None
        if log:
            self.log = MagicMock()
            self.log.post_admin_override = AsyncMock()
            self.log.post_error = AsyncMock()
            self.log.post_interaction = AsyncMock()
        self.get_locks = AsyncMock(return_value=locks or {})
        self.acquire = AsyncMock(return_value=True)
        self.release = AsyncMock()
        self.init_slots = AsyncMock()
        self.force_book = AsyncMock()
        self.ensure = AsyncMock(return_value="holder-dj-page")
        mod = "bot.services.notion_sync"
        monkeypatch.setattr(f"{mod}.get_slot_locks_for_events", self.get_locks)
        monkeypatch.setattr(f"{mod}.acquire_slot_lock", self.acquire)
        monkeypatch.setattr(f"{mod}.release_slot_lock", self.release)
        monkeypatch.setattr(f"{mod}.init_event_slots", self.init_slots)
        monkeypatch.setattr(f"{mod}.ensure_dj_page", self.ensure)
        self.svc = NotionSyncService(self.bot, self.db, self.notion, self.log)

    def overrides(self) -> list[str]:
        assert self.log is not None
        return [c.args[0] for c in self.log.post_admin_override.await_args_list]

    def assert_no_writes(self):
        for name in (
            "create_slot_rows", "trash_slot_rows", "force_assign_slot", "keep_first_dj",
            "assign_slot", "clear_slot", "upsert_dj",
        ):
            getattr(self.notion, name).assert_not_awaited()
        self.acquire.assert_not_awaited()
        self.release.assert_not_awaited()
        self.init_slots.assert_not_awaited()
        self.user.send.assert_not_awaited()


def unix(event, index: int) -> int:
    return int(event.slot_start(index).timestamp())


# ---- adopt ----


@pytest.mark.asyncio
async def test_adopt_writes_lock_dms_and_logs_one_line(monkeypatch):
    event = event_with(dj_row(1, X), make_slot(2))
    env = Env(monkeypatch, [event])
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["adopt"]
    env.acquire.assert_awaited_once_with(env.db, PAGE, 0, X)
    env.user.send.assert_awaited_once_with(
        f"An organizer has added you to the lineup for Rave at <t:{unix(event, 0)}:f>."
    )
    assert env.overrides() == [f"[NOTION SYNC] {X} was added to Rave slot 1 in Notion"]
    env.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_when_lock_taken_meanwhile_does_nothing_else(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, X), make_slot(2))])
    env.acquire.return_value = False
    await env.svc.run_cycle(1)
    env.acquire.assert_awaited_once()
    env.user.send.assert_not_awaited()
    env.log.post_admin_override.assert_not_awaited()
    env.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_dj_produces_no_lock_and_no_dm(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, None, page="guest"), make_slot(2))])
    assert await env.svc.run_cycle(1) == []
    env.assert_no_writes()
    env.log.post_admin_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_failing_dm_does_not_fail_cycle_or_undo_adopt(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, X), make_slot(2))])
    env.user.send.side_effect = RuntimeError("discord down")
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["adopt"]
    env.acquire.assert_awaited_once()
    env.release.assert_not_awaited()
    assert len(env.overrides()) == 1


# ---- free ----


@pytest.mark.asyncio
async def test_free_releases_lock_dms_and_logs(monkeypatch):
    event = event_with(make_slot(1), make_slot(2))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(Y)})
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["free"]
    env.release.assert_awaited_once_with(env.db, PAGE, 0)
    env.bot.get_user.assert_any_call(Y)
    env.user.send.assert_awaited_once_with(
        f"Your booking for Rave at <t:{unix(event, 0)}:f> was removed in Notion."
    )
    assert env.overrides() == [f"[NOTION SYNC] {Y} was removed from Rave slot 1 in Notion"]


# ---- revert ----


@pytest.mark.asyncio
async def test_revert_restores_holder_in_notion_without_dm(monkeypatch):
    event = event_with(dj_row(1, Y, page="other-page"), make_slot(2))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)})
    actions = await env.svc.run_cycle(1)
    assert [(a.kind, a.discord_id) for a in actions] == [("revert", X)]
    env.ensure.assert_awaited_once_with(env.db, env.notion, X, "holder-name")
    env.notion.force_assign_slot.assert_awaited_once_with(event, 0, "holder-dj-page")
    env.user.send.assert_not_awaited()
    env.acquire.assert_not_awaited()
    env.release.assert_not_awaited()
    assert env.overrides() == [
        f"[NOTION SYNC] a hand edit on slot 1 of Rave was reverted to {X}"
    ]


@pytest.mark.asyncio
async def test_revert_uses_id_as_name_when_user_unknown(monkeypatch):
    event = event_with(dj_row(1, Y), make_slot(2))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)})
    env.bot.get_user.return_value = None
    await env.svc.run_cycle(1)
    env.ensure.assert_awaited_once_with(env.db, env.notion, X, str(X))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "raise"])
async def test_revert_without_dj_page_posts_error_and_leaves_notion(monkeypatch, failure):
    event = event_with(dj_row(1, Y), make_slot(2))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)})
    if failure == "none":
        env.ensure.return_value = None
    else:
        env.ensure.side_effect = RuntimeError("notion down")
    await env.svc.run_cycle(1)
    env.notion.force_assign_slot.assert_not_awaited()
    env.log.post_error.assert_awaited_once()
    assert env.log.post_error.await_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE
    env.log.post_admin_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_revert_extras_keeps_first_dj(monkeypatch):
    event = event_with(dj_row(1, X, extras=("extra",)), make_slot(2))
    env = Env(monkeypatch, [event])
    actions = await env.svc.run_cycle(1)
    assert [(a.kind, a.discord_id) for a in actions] == [("revert", None)]
    env.notion.keep_first_dj.assert_awaited_once_with(event, 0)
    env.notion.force_assign_slot.assert_not_awaited()
    env.acquire.assert_not_awaited()
    env.user.send.assert_not_awaited()
    assert len(env.overrides()) == 1


# ---- event-level ----


@pytest.mark.asyncio
async def test_create_slots_creates_missing_rows_and_inits_locks(monkeypatch):
    event = event_with(make_slot(1), slot_count=3)
    env = Env(monkeypatch, [event])
    await env.svc.run_cycle(1)
    env.notion.create_slot_rows.assert_awaited_once_with(event, [2, 3])
    env.init_slots.assert_awaited_once_with(env.db, PAGE, 3)
    assert len(env.overrides()) == 1


@pytest.mark.asyncio
async def test_trim_slots_trashes_only_named_rows(monkeypatch):
    rows = [make_slot(1), make_slot(2), make_slot(3)]
    event = event_with(*rows, slot_count=1)
    env = Env(monkeypatch, [event])
    await env.svc.run_cycle(1)
    env.notion.trash_slot_rows.assert_awaited_once_with([rows[1], rows[2]])
    assert len(env.overrides()) == 1


@pytest.mark.asyncio
async def test_skip_is_logged_once_across_cycles_and_not_to_admin(monkeypatch, caplog):
    event = event_with(make_slot(1), dj_row(2, None, page="guest"), slot_count=1)
    env = Env(monkeypatch, [event])
    with caplog.at_level(logging.WARNING, logger="bot.services.notion_sync"):
        first = await env.svc.run_cycle(1)
        second = await env.svc.run_cycle(1)
    assert [a.kind for a in first] == [a.kind for a in second] == ["skip"]
    hits = [r for r in caplog.records if first[0].detail in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING
    env.log.post_admin_override.assert_not_awaited()
    env.log.post_error.assert_not_awaited()
    env.assert_no_writes()


# ---- robustness ----


@pytest.mark.asyncio
async def test_failing_action_does_not_stop_next_action(monkeypatch):
    event = event_with(dj_row(1, Y), dj_row(2, X))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)})
    env.notion.force_assign_slot.side_effect = SlotRowMissingError("Rave", 1)
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["revert", "adopt"]
    env.acquire.assert_awaited_once_with(env.db, PAGE, 1, X)
    env.log.post_error.assert_awaited_once()
    assert env.log.post_error.await_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE


@pytest.mark.asyncio
async def test_failing_db_action_does_not_stop_next_action(monkeypatch):
    event = event_with(dj_row(1, X), dj_row(2, Y))
    env = Env(monkeypatch, [event])
    env.acquire.side_effect = [RuntimeError("db down"), True]
    await env.svc.run_cycle(1)
    assert env.acquire.await_count == 2
    env.user.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notion_write_error_is_not_posted_twice(monkeypatch):
    """NotionApi already posts ERR_NOTION_WRITE before raising NotionWriteError."""
    event = event_with(make_slot(1), slot_count=2)
    env = Env(monkeypatch, [event])
    env.notion.create_slot_rows.side_effect = NotionWriteError("boom")
    await env.svc.run_cycle(1)
    env.log.post_error.assert_not_awaited()
    env.init_slots.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_cycle_over_unchanged_state_writes_nothing(monkeypatch):
    event = event_with(dj_row(1, X), make_slot(2))
    env = Env(monkeypatch, [event])
    env.get_locks.side_effect = [{}, {(PAGE, 0): lock(X)}]
    await env.svc.run_cycle(1)
    env.acquire.reset_mock()
    env.user.send.reset_mock()
    env.log.post_admin_override.reset_mock()
    assert await env.svc.run_cycle(1) == []
    env.assert_no_writes()
    env.log.post_admin_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_query_uses_event_page_ids(monkeypatch):
    events = [event_with(make_slot(1), make_slot(2)), event_with(page_id="p2", event_id="B")]
    env = Env(monkeypatch, events)
    env.notion.list_events.return_value = events
    await env.svc.run_cycle(1)
    env.get_locks.assert_awaited_once_with(env.db, [PAGE, "p2"])


@pytest.mark.asyncio
async def test_list_events_failure_propagates(monkeypatch):
    env = Env(monkeypatch, [])
    env.notion.list_events.side_effect = NotionWriteError("down")
    with pytest.raises(NotionWriteError):
        await env.svc.run_cycle(1)


@pytest.mark.asyncio
async def test_lock_query_failure_propagates(monkeypatch):
    env = Env(monkeypatch, [event_with(make_slot(1), make_slot(2))])
    env.get_locks.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        await env.svc.run_cycle(1)


@pytest.mark.asyncio
async def test_missing_log_service_does_not_break(monkeypatch):
    event = event_with(dj_row(1, X), dj_row(2, Y), make_slot(3), slot_count=4)
    env = Env(monkeypatch, [event], {(PAGE, 1): lock(X)}, log=False)
    env.ensure.return_value = None  # revert of slot 2 fails -> would post_error
    actions = await env.svc.run_cycle(1)
    assert {a.kind for a in actions} == {"create_slots", "adopt", "revert"}
    env.acquire.assert_awaited_once()
    env.notion.create_slot_rows.assert_awaited_once()


@pytest.mark.asyncio
async def test_trim_slots_never_trashes_a_duplicate_row_holding_a_dj(monkeypatch):
    empty = make_slot(2, page_id="a-dup")
    held = make_slot(2, page_id="b-dup", dj_page_id="dj", discord_id=None)
    event = event_with(make_slot(1), empty, held, slot_count=1)
    env = Env(monkeypatch, [event])
    await env.svc.run_cycle(1)
    env.notion.trash_slot_rows.assert_awaited_once_with([empty])
