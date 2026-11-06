"""Tests for the pure Notion/Postgres sync planner (plan_sync)."""
import datetime
import random

import pytest

from bot.db.bookings import SlotLock
from bot.services.notion_sync_plan import SyncAction, plan_sync
from tests.notion.factories import make_event, make_slot

NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC)
OLD = NOW - datetime.timedelta(seconds=600)
X, Y = 111, 222


def lock(holder: int | None, age_s: float | None = 600) -> SlotLock:
    booked = None if age_s is None else NOW - datetime.timedelta(seconds=age_s)
    return SlotLock(discord_user_id=holder, booked_at=booked)


def full_rows(n: int, **overrides: dict) -> list:
    """Slot rows 1..n, all empty unless overridden by number."""
    return [make_slot(i, **overrides.get(str(i), {})) for i in range(1, n + 1)]


def dj(page: str, discord_id: int | None, extras: tuple[str, ...] = ()) -> dict:
    return {
        "dj_page_id": page,
        "dj_name": page,
        "discord_id": discord_id,
        "extra_dj_page_ids": extras,
    }


def kinds(actions: list[SyncAction]) -> list[tuple]:
    return [(a.kind, a.slot_index, a.discord_id, a.numbers) for a in actions]


# ---- single-slot rules (event with 2 slots, rows 1 and 2; decisions on slot 1) ----

CASES = [
    # id, slot-1 row overrides, lock on index 0, expected actions
    ("empty_no_lock", {}, None, []),
    ("empty_lock_row_without_holder", {}, lock(None), []),
    ("empty_old_holder_frees", {}, lock(X, 600), [("free", 0, X, ())]),
    ("empty_holder_119s_kept", {}, lock(X, 119), []),
    ("empty_holder_120s_kept", {}, lock(X, 120), []),
    ("empty_holder_121s_frees", {}, lock(X, 121), [("free", 0, X, ())]),
    ("empty_holder_booked_at_none_frees", {}, lock(X, None), [("free", 0, X, ())]),
    ("dj_no_holder_adopts", dj("dj-x", X), None, [("adopt", 0, X, ())]),
    ("dj_lock_row_without_holder_adopts", dj("dj-x", X), lock(None), [("adopt", 0, X, ())]),
    ("dj_same_holder_nothing", dj("dj-x", X), lock(X), []),
    ("dj_same_holder_fresh_nothing", dj("dj-x", X), lock(X, 1), []),
    ("dj_other_holder_reverts", dj("dj-x", X), lock(Y), [("revert", 0, Y, ())]),
    ("dj_other_holder_fresh_reverts", dj("dj-x", X), lock(Y, 1), [("revert", 0, Y, ())]),
    ("guest_no_holder_nothing", dj("dj-g", None), None, []),
    ("guest_with_holder_reverts", dj("dj-g", None), lock(Y), [("revert", 0, Y, ())]),
    ("guest_with_fresh_holder_reverts", dj("dj-g", None), lock(Y, 1), [("revert", 0, Y, ())]),
    ("extras_with_holder_reverts_to_holder", dj("dj-x", X, ("dj-z",)), lock(Y),
     [("revert", 0, Y, ())]),
    ("extras_first_is_holder_reverts_to_holder", dj("dj-x", X, ("dj-z",)), lock(X),
     [("revert", 0, X, ())]),
    ("extras_no_holder_clears_extras", dj("dj-x", X, ("dj-z",)), None,
     [("revert", 0, None, ())]),
    ("extras_guest_first_no_holder_clears_extras", dj("dj-g", None, ("dj-z",)), None,
     [("revert", 0, None, ())]),
]


@pytest.mark.parametrize(
    ("row", "slot_lock", "expected"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_single_slot_rules(row: dict, slot_lock: SlotLock | None, expected: list) -> None:
    event = make_event(page_id="ev", slot_count=2, slots=full_rows(2, **{"1": row}))
    locks = {("ev", 0): slot_lock} if slot_lock is not None else {}
    actions = plan_sync([event], locks, NOW)
    assert kinds(actions) == expected
    assert all(a.event is event and a.detail for a in actions)


def test_actions_use_lock_on_matching_index_only() -> None:
    # Slot 2 (index 1) has DJ X, lock is on index 1 for X: nothing.
    event = make_event(page_id="ev", slot_count=2, slots=full_rows(2, **{"2": dj("dj-x", X)}))
    assert plan_sync([event], {("ev", 1): lock(X)}, NOW) == []


def test_same_dj_holding_two_slots_yields_nothing() -> None:
    event = make_event(
        page_id="ev", slot_count=3,
        slots=full_rows(3, **{"1": dj("dj-x", X), "2": dj("dj-x", X)}),
    )
    locks = {("ev", 0): lock(X), ("ev", 1): lock(X), ("ev", 2): lock(None)}
    assert plan_sync([event], locks, NOW) == []


def test_same_dj_two_slots_one_unlocked_adopts_only_that_one() -> None:
    event = make_event(
        page_id="ev", slot_count=2, slots=full_rows(2, **{"1": dj("dj-x", X), "2": dj("dj-x", X)}),
    )
    assert kinds(plan_sync([event], {("ev", 0): lock(X)}, NOW)) == [("adopt", 1, X, ())]


# ---- missing rows / slot_count changes -------------------------------------------


def test_event_without_rows_creates_all_and_nothing_else() -> None:
    event = make_event(page_id="ev", slot_count=3, slots=())
    # Locks on the missing rows must not produce free actions this cycle.
    actions = plan_sync([event], {("ev", 0): lock(X), ("ev", 2): lock(Y, None)}, NOW)
    assert kinds(actions) == [("create_slots", None, None, (1, 2, 3))]


def test_missing_rows_only_for_gaps_other_slots_still_decided() -> None:
    event = make_event(
        page_id="ev", slot_count=4,
        slots=[make_slot(1, **dj("dj-x", X)), make_slot(3)],
    )
    actions = plan_sync([event], {("ev", 1): lock(Y)}, NOW)
    assert kinds(actions) == [
        ("create_slots", None, None, (2, 4)),
        ("adopt", 0, X, ()),
    ]


def test_two_events_one_with_missing_rows() -> None:
    a = make_event(event_id="A", page_id="ev-a", date=datetime.date(2026, 12, 1),
                   slot_count=2, slots=full_rows(2, **{"2": dj("dj-x", X)}))
    b = make_event(event_id="B", page_id="ev-b", date=datetime.date(2026, 12, 2),
                   slot_count=2, slots=())
    actions = plan_sync([b, a], {}, NOW)
    assert [(x.event.page_id, x.kind, x.slot_index, x.numbers) for x in actions] == [
        ("ev-a", "adopt", 1, ()),
        ("ev-b", "create_slots", None, (1, 2)),
    ]


def test_slot_count_increased_creates_new_numbers() -> None:
    event = make_event(page_id="ev", slot_count=5, slots=full_rows(3))
    assert kinds(plan_sync([event], {}, NOW)) == [("create_slots", None, None, (4, 5))]


def test_slot_count_decreased_trims_empty_rows_and_skips_held_rows() -> None:
    event = make_event(
        page_id="ev", slot_count=2,
        slots=full_rows(5, **{"4": dj("dj-x", X), "5": dj("dj-g", None)}),
    )
    actions = plan_sync([event], {}, NOW)
    assert kinds(actions) == [
        ("skip", 3, None, (4,)),
        ("skip", 4, None, (5,)),
        ("trim_slots", None, None, (3,)),
    ]
    assert "#4" in actions[0].detail and "#5" in actions[1].detail


def test_row_above_count_with_lock_but_no_dj_is_skipped_not_trimmed() -> None:
    event = make_event(page_id="ev", slot_count=1, slots=full_rows(3))
    actions = plan_sync([event], {("ev", 1): lock(X), ("ev", 2): lock(None)}, NOW)
    assert kinds(actions) == [
        ("skip", 1, None, (2,)),
        ("trim_slots", None, None, (3,)),
    ]


def test_row_above_count_with_extra_dj_only_is_skipped() -> None:
    event = make_event(page_id="ev", slot_count=1,
                       slots=full_rows(2, **{"2": dj("dj-x", X, ("dj-z",))}))
    assert kinds(plan_sync([event], {}, NOW)) == [("skip", 1, None, (2,))]


def test_rows_with_number_zero_or_negative_are_ignored() -> None:
    event = make_event(
        page_id="ev", slot_count=1,
        slots=[make_slot(0, **dj("dj-x", X), page_id="zero"),
               make_slot(-1, page_id="neg"), make_slot(1)],
    )
    assert plan_sync([event], {("ev", -1): lock(X)}, NOW) == []


def test_locks_for_unknown_events_ignored() -> None:
    event = make_event(page_id="ev", slot_count=1, slots=full_rows(1))
    assert plan_sync([event], {("other", 0): lock(X, None)}, NOW) == []
    assert plan_sync([], {("other", 0): lock(X, None)}, NOW) == []


def test_lock_beyond_slot_count_without_row_ignored() -> None:
    event = make_event(page_id="ev", slot_count=1, slots=full_rows(1))
    assert plan_sync([event], {("ev", 5): lock(X, None)}, NOW) == []


# ---- ordering ----------------------------------------------------------------------


def _many_events() -> list:
    return [
        make_event(event_id="Late", page_id="ev-late", date=datetime.date(2026, 12, 1),
                   start=datetime.time(22, 0), slot_count=3,
                   slots=[make_slot(3, **dj("dj-y", Y)), make_slot(1, **dj("dj-x", X))]),
        make_event(event_id="Early", page_id="ev-early", date=datetime.date(2026, 12, 1),
                   start=datetime.time(18, 0), slot_count=2,
                   slots=full_rows(4, **{"2": dj("dj-x", X), "4": dj("dj-y", Y)})),
        make_event(event_id="Next", page_id="ev-next", date=datetime.date(2026, 12, 8),
                   slot_count=2, slots=full_rows(2)),
    ]


def test_output_ordered_by_event_date_start_then_slot() -> None:
    locks = {("ev-next", 1): lock(X, None), ("ev-late", 2): lock(X)}
    actions = plan_sync(_many_events(), locks, NOW)
    assert [(a.event.page_id, a.kind, a.slot_index, a.discord_id, a.numbers)
            for a in actions] == [
        ("ev-early", "adopt", 1, X, ()),
        ("ev-early", "skip", 3, None, (4,)),
        ("ev-early", "trim_slots", None, None, (3,)),
        ("ev-late", "create_slots", None, None, (2,)),
        ("ev-late", "adopt", 0, X, ()),
        ("ev-late", "revert", 2, X, ()),
        ("ev-next", "free", 1, X, ()),
    ]


def test_shuffled_input_gives_same_output() -> None:
    import dataclasses

    events = _many_events()
    locks = {("ev-next", 1): lock(X, None), ("ev-late", 2): lock(X)}
    expected = plan_sync(events, locks, NOW)
    rng = random.Random(7)
    for _ in range(10):
        shuffled = []
        for e in events:
            rows = list(e.slots)
            rng.shuffle(rows)
            shuffled.append(dataclasses.replace(e, slots=tuple(rows)))
        rng.shuffle(shuffled)
        lock_items = list(locks.items())
        rng.shuffle(lock_items)
        got = plan_sync(shuffled, dict(lock_items), NOW)
        assert [(a.event.page_id, a.kind, a.slot_index, a.discord_id, a.numbers, a.detail)
                for a in got] == [
            (a.event.page_id, a.kind, a.slot_index, a.discord_id, a.numbers, a.detail)
            for a in expected
        ]


def test_sync_action_is_frozen_with_defaults() -> None:
    event = make_event()
    action = SyncAction(kind="skip", event=event, slot_index=None, detail="d")
    assert action.discord_id is None and action.numbers == ()
    with pytest.raises(AttributeError):
        action.kind = "adopt"  # type: ignore[misc]


# ---- event shape (Start / Slot length) -------------------------------------------


def _shape(page_id: str = "ev", start: str = "20:00", minutes: int = 60, count: int = 2):
    from bot.db.event_shapes import EventShapeRow

    return {page_id: EventShapeRow(page_id, start, minutes, count)}


def _shape_kinds(actions: list[SyncAction]) -> list:
    return [(a.kind, a.restore) for a in actions if a.kind.endswith("_shape")]


def test_shape_first_sight_is_accepted() -> None:
    event = make_event(page_id="ev", slot_count=2, slots=full_rows(2, **{"1": dj("dj-x", X)}))
    actions = plan_sync([event], {("ev", 0): lock(X)}, NOW, {})
    assert _shape_kinds(actions) == [("accept_shape", None)]
    assert actions[0].kind == "accept_shape" and actions[0].slot_index is None


def test_shape_unchanged_emits_nothing() -> None:
    event = make_event(page_id="ev", slot_count=2, slots=full_rows(2, **{"1": dj("dj-x", X)}))
    assert plan_sync([event], {("ev", 0): lock(X)}, NOW, _shape()) == []


def test_shape_without_shapes_argument_emits_nothing() -> None:
    event = make_event(page_id="ev", slot_count=2, slots=full_rows(2))
    assert plan_sync([event], {}, NOW) == []


@pytest.mark.parametrize("start,minutes", [
    (datetime.time(21, 0), 60), (datetime.time(20, 0), 90), (datetime.time(22, 30), 45),
])
def test_shape_change_without_dj_is_accepted(start, minutes) -> None:
    event = make_event(page_id="ev", slot_count=2, start=start, slot_minutes=minutes,
                       slots=full_rows(2))
    actions = plan_sync([event], {("ev", 0): lock(None)}, NOW, _shape())
    assert kinds(actions) == [("accept_shape", None, None, ())]


@pytest.mark.parametrize("start,minutes", [
    (datetime.time(21, 0), 60), (datetime.time(20, 0), 90), (datetime.time(22, 30), 45),
])
def test_shape_change_with_dj_is_restored(start, minutes) -> None:
    event = make_event(page_id="ev", slot_count=2, start=start, slot_minutes=minutes,
                       slots=full_rows(2, **{"2": dj("dj-g", None)}))
    actions = plan_sync([event], {}, NOW, _shape())
    assert _shape_kinds(actions) == [("restore_shape", (datetime.time(20, 0), 60))]
    assert actions[0].event is event and actions[0].slot_index is None
    assert event.event_id in actions[0].detail


def test_shape_restore_counts_dj_on_row_above_slot_count() -> None:
    event = make_event(page_id="ev", slot_count=1, start=datetime.time(21, 0),
                       slots=full_rows(2, **{"2": dj("dj-x", X)}))
    actions = plan_sync([event], {}, NOW, _shape(count=2))
    assert [a.kind for a in actions] == ["restore_shape", "skip"]


def test_shape_restore_later_slot_actions_use_stored_times() -> None:
    event = make_event(page_id="ev", slot_count=2, start=datetime.time(23, 0),
                       slots=full_rows(2, **{"1": dj("dj-x", X), "2": dj("dj-y", Y)}))
    actions = plan_sync([event], {("ev", 0): lock(X)}, NOW, _shape())
    assert [a.kind for a in actions] == ["restore_shape", "adopt"]
    assert actions[1].event.start == datetime.time(20, 0)
    assert actions[1].event.slot_minutes == 60


def test_shape_slot_count_change_is_accepted_and_keeps_create_behaviour() -> None:
    event = make_event(page_id="ev", slot_count=3, slots=full_rows(2, **{"1": dj("dj-x", X)}))
    actions = plan_sync([event], {("ev", 0): lock(X)}, NOW, _shape(count=2))
    assert kinds(actions) == [
        ("accept_shape", None, None, ()),
        ("create_slots", None, None, (3,)),
    ]


def test_shape_slot_count_change_keeps_trim_behaviour() -> None:
    event = make_event(page_id="ev", slot_count=1, slots=full_rows(2, **{"1": dj("dj-x", X)}))
    actions = plan_sync([event], {("ev", 0): lock(X)}, NOW, _shape(count=2))
    assert kinds(actions) == [
        ("accept_shape", None, None, ()),
        ("trim_slots", None, None, (2,)),
    ]
