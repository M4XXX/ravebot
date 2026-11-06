"""Tests for NotionSyncService.run_cycle (applies plan_sync actions)."""
import datetime
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.bookings import SlotLock
from bot.db.event_shapes import EventShapeRow
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
    def __init__(self, monkeypatch, events, locks=None, *, log=True, shapes=None):
        self.user = MagicMock()
        self.user.send = AsyncMock()
        self.user.__str__ = MagicMock(return_value="holder-username")
        self.user.display_name = "holder-name"
        self.bot = MagicMock()
        self.bot.get_user = MagicMock(return_value=self.user)
        self.bot.fetch_user = AsyncMock(return_value=self.user)
        self.db = MagicMock(name="pool")
        self.notion = MagicMock()
        self.notion.list_events = AsyncMock(return_value=events)
        for name in (
            "create_slot_rows", "trash_slot_rows", "force_assign_slot", "keep_first_dj",
            "assign_slot", "clear_slot", "upsert_dj", "restore_event_shape",
        ):
            setattr(self.notion, name, AsyncMock())
        self.log = None
        if log:
            self.log = MagicMock()
            for name in (
                "post_error", "log_booked",
                "log_cancelled", "log_changed_by_organizer", "log_changed_in_notion",
                "log_attention",
            ):
                setattr(self.log, name, AsyncMock())
        self.get_locks = AsyncMock(return_value=locks or {})
        self.acquire = AsyncMock(return_value=True)
        self.release = AsyncMock()
        self.init_slots = AsyncMock()
        self.force_book = AsyncMock()
        self.ensure = AsyncMock(return_value="holder-dj-page")
        if shapes is None:  # default: stored shapes equal the current ones
            shapes = {
                e.page_id: EventShapeRow(
                    e.page_id, e.start.strftime("%H:%M"), e.slot_minutes, e.slot_count
                )
                for e in events
            }
        self.get_shapes = AsyncMock(return_value=shapes)
        self.upsert_shape = AsyncMock()
        mod = "bot.services.notion_sync"
        monkeypatch.setattr(f"{mod}.get_event_shapes", self.get_shapes)
        monkeypatch.setattr(f"{mod}.upsert_event_shape", self.upsert_shape)
        monkeypatch.setattr(f"{mod}.get_slot_locks_for_events", self.get_locks)
        monkeypatch.setattr(f"{mod}.acquire_slot_lock", self.acquire)
        monkeypatch.setattr(f"{mod}.release_slot_lock", self.release)
        monkeypatch.setattr(f"{mod}.init_event_slots", self.init_slots)
        monkeypatch.setattr(f"{mod}.ensure_dj_page", self.ensure)
        self.svc = NotionSyncService(self.bot, self.db, self.notion, self.log)

    def entries(self) -> list[tuple[str, tuple]]:
        """Every channel entry the sync posted (post_error excluded)."""
        assert self.log is not None
        return [(c[0], c[1]) for c in self.log.mock_calls if c[0] != "post_error"]

    def assert_no_writes(self):
        for name in (
            "create_slot_rows", "trash_slot_rows", "force_assign_slot", "keep_first_dj",
            "assign_slot", "clear_slot", "upsert_dj", "restore_event_shape",
        ):
            getattr(self.notion, name).assert_not_awaited()
        self.upsert_shape.assert_not_awaited()
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
        "You are in the lineup: Rave, slot 1 at 20:00 (added by an organizer)."
    )
    assert env.entries() == [("log_booked", ("holder-name", event, 0, "Notion"))]
    env.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_when_lock_taken_meanwhile_does_nothing_else(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, X), make_slot(2))])
    env.acquire.return_value = False
    await env.svc.run_cycle(1)
    env.acquire.assert_awaited_once()
    env.user.send.assert_not_awaited()
    assert env.entries() == []
    env.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_dj_produces_no_lock_and_no_dm(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, None, page="guest"), make_slot(2))])
    assert await env.svc.run_cycle(1) == []
    env.assert_no_writes()
    assert env.entries() == []


@pytest.mark.asyncio
async def test_guest_dj_over_holder_is_reverted_to_holder(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, None, page="guest"), make_slot(2))],
              {(PAGE, 0): lock(Y)})
    actions = await env.svc.run_cycle(1)
    assert [(a.kind, a.discord_id) for a in actions] == [("revert", Y)]
    env.notion.force_assign_slot.assert_awaited_once_with(actions[0].event, 0, "holder-dj-page")
    env.release.assert_not_awaited()
    env.user.send.assert_not_awaited()
    assert env.entries() == [(
        "log_attention",
        (
            "Hand edit reverted",
            "A hand edit on slot 1 at 20:00 of **Rave** was reverted to holder-name.",
            "Clear the slot in Notion first if you want to replace a DJ.",
        ),
    )]


@pytest.mark.asyncio
async def test_failing_dm_does_not_fail_cycle_or_undo_adopt(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, X), make_slot(2))])
    env.user.send.side_effect = RuntimeError("discord down")
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["adopt"]
    env.acquire.assert_awaited_once()
    env.release.assert_not_awaited()
    assert [e[0] for e in env.entries()] == ["log_booked"]


# ---- free ----


@pytest.mark.asyncio
async def test_free_releases_lock_dms_and_logs(monkeypatch):
    event = event_with(make_slot(1), make_slot(2))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(Y)})
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["free"]
    env.release.assert_awaited_once_with(env.db, PAGE, 0, expected_holder=Y)
    env.bot.get_user.assert_any_call(Y)
    env.user.send.assert_awaited_once_with(
        "You were removed from Rave, slot 1 at 20:00 (by an organizer)."
    )
    assert env.entries() == [("log_changed_in_notion", ("holder-name", event, 0))]


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
    assert env.entries() == [(
        "log_attention",
        (
            "Hand edit reverted",
            "A hand edit on slot 1 at 20:00 of **Rave** was reverted to holder-name.",
            "Clear the slot in Notion first if you want to replace a DJ.",
        ),
    )]


@pytest.mark.asyncio
async def test_revert_passes_empty_name_when_user_uncached(monkeypatch):
    event = event_with(dj_row(1, Y), make_slot(2))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)})
    env.bot.get_user.return_value = None
    await env.svc.run_cycle(1)
    env.ensure.assert_awaited_once_with(env.db, env.notion, X, "")
    env.notion.refresh_discord_name.assert_not_called()


@pytest.mark.asyncio
async def test_revert_passes_display_name_not_str_user(monkeypatch):
    event = event_with(dj_row(1, Y), make_slot(2))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)})
    await env.svc.run_cycle(1)
    args = env.ensure.await_args.args
    assert args[3] == "holder-name" != str(env.user)


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
    assert env.entries() == []


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
    assert env.entries() == []


# ---- event-level ----


@pytest.mark.asyncio
async def test_create_slots_creates_missing_rows_and_inits_locks(monkeypatch):
    event = event_with(make_slot(1), slot_count=3)
    env = Env(monkeypatch, [event])
    await env.svc.run_cycle(1)
    env.notion.create_slot_rows.assert_awaited_once_with(event, [2, 3])
    env.init_slots.assert_awaited_once_with(env.db, PAGE, 3)
    assert env.entries() == []


@pytest.mark.asyncio
async def test_trim_slots_trashes_only_named_rows(monkeypatch):
    rows = [make_slot(1), make_slot(2), make_slot(3)]
    event = event_with(*rows, slot_count=1)
    env = Env(monkeypatch, [event])
    await env.svc.run_cycle(1)
    env.notion.trash_slot_rows.assert_awaited_once_with([rows[1], rows[2]])
    assert env.entries() == []


@pytest.mark.asyncio
async def test_skip_is_logged_and_posted_once_across_cycles(monkeypatch, caplog):
    event = event_with(make_slot(1), dj_row(2, None, page="guest"), slot_count=1)
    env = Env(monkeypatch, [event])
    with caplog.at_level(logging.WARNING, logger="bot.services.notion_sync"):
        first = await env.svc.run_cycle(1)
        second = await env.svc.run_cycle(1)
    assert [a.kind for a in first] == [a.kind for a in second] == ["skip"]
    hits = [r for r in caplog.records if first[0].detail in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING
    assert env.entries() == []
    env.log.post_error.assert_not_awaited()
    env.assert_no_writes()


@pytest.mark.asyncio
async def test_skip_without_log_service_only_logs(monkeypatch, caplog):
    event = event_with(make_slot(1), dj_row(2, None, page="guest"), slot_count=1)
    env = Env(monkeypatch, [event], log=False)
    with caplog.at_level(logging.WARNING, logger="bot.services.notion_sync"):
        actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["skip"]
    assert any(actions[0].detail in r.getMessage() for r in caplog.records)


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
    env.log.reset_mock()
    assert await env.svc.run_cycle(1) == []
    env.assert_no_writes()
    assert env.entries() == []


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


# ---- event shape (Start / Slot length) ----


def shape(start="20:00", minutes=60, count=2, page=PAGE):
    return {page: EventShapeRow(page, start, minutes, count)}


@pytest.mark.asyncio
async def test_cycle_reads_stored_shapes_for_listed_events(monkeypatch):
    env = Env(monkeypatch, [event_with(make_slot(1), make_slot(2))])
    await env.svc.run_cycle(1)
    env.get_shapes.assert_awaited_once_with(env.db, [PAGE])


@pytest.mark.asyncio
async def test_accept_shape_stores_current_shape(monkeypatch):
    event = make_event(event_id="Rave", page_id=PAGE, slot_count=2,
                       start=datetime.time(21, 30), slot_minutes=45,
                       slots=(make_slot(1), make_slot(2)))
    env = Env(monkeypatch, [event], shapes={})
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["accept_shape"]
    env.upsert_shape.assert_awaited_once_with(env.db, PAGE, "21:30", 45, 2)
    env.notion.restore_event_shape.assert_not_awaited()
    assert env.entries() == []
    env.user.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_shape_patches_notion_and_logs_one_line_without_dm(monkeypatch):
    event = make_event(event_id="Rave", page_id=PAGE, slot_count=2,
                       start=datetime.time(22, 0), slot_minutes=90,
                       slots=(dj_row(1, X), make_slot(2)))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)}, shapes=shape())
    actions = await env.svc.run_cycle(1)
    assert [a.kind for a in actions] == ["restore_shape"]
    env.notion.restore_event_shape.assert_awaited_once_with(
        event, datetime.time(20, 0), 60
    )
    assert env.entries() == []
    env.upsert_shape.assert_not_awaited()
    env.user.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_shape_failure_posts_error_and_no_line(monkeypatch):
    event = make_event(event_id="Rave", page_id=PAGE, slot_count=2,
                       start=datetime.time(22, 0), slots=(dj_row(1, X), make_slot(2)))
    env = Env(monkeypatch, [event], {(PAGE, 0): lock(X)}, shapes=shape())
    env.notion.restore_event_shape.side_effect = RuntimeError("boom")
    await env.svc.run_cycle(1)
    assert env.entries() == []
    env.log.post_error.assert_awaited_once()
    env.upsert_shape.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopt_free_and_revert_work_without_log_service(monkeypatch):
    env = Env(monkeypatch, [event_with(dj_row(1, X), make_slot(2))], log=False)
    assert [a.kind for a in await env.svc.run_cycle(1)] == ["adopt"]
    env = Env(monkeypatch, [event_with(make_slot(1), make_slot(2))], {(PAGE, 0): lock(Y)},
              log=False)
    assert [a.kind for a in await env.svc.run_cycle(1)] == ["free"]
    env = Env(monkeypatch, [event_with(dj_row(1, Y, page="other"), make_slot(2))],
              {(PAGE, 0): lock(X)}, log=False)
    assert [a.kind for a in await env.svc.run_cycle(1)] == ["revert"]
