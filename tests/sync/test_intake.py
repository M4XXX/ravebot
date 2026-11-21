"""Tests for bot/sync/intake.py: checked hand edits from Notion applied to Postgres.

The repositories are replaced by an in-memory store; the real SQL of edit_event is
covered in tests/pg/test_events_repo.py.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.events import DuplicateEventError, EventDraft, SlotsBookedError
from bot.notion.models import EventInfo, SlotInfo
from bot.sync import intake as intake_mod
from bot.sync.intake import Intake
from bot.sync.plan import Change

TODAY = datetime.date(2026, 11, 20)
DAY = "2026-12-01"
A = 111111111111111111
B = 222222222222222222


def event_payload(**over):
    payload = {
        "event_id": "2026-12-01 Rave Night", "date": DAY, "start": "20:00",
        "slot_minutes": 60, "slot_count": 3, "status": None, "genre": None, "notes": None,
        "location": None,
    }
    payload.update(over)
    return payload


def dj_payload(name="DJ New", did=A, link="https://twitch.tv/new"):
    return {"discord_id": did, "dj_name": name, "vrchat_name": None, "link": link, "alt_link": None}


class Store:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.holders: dict[tuple[str, int], int | None] = {}
        self.djs: dict[int, dict] = {}
        self.synced: dict[tuple[str, str], tuple[str, dict]] = {}
        self.changed: list[tuple[str, str]] = []
        self.manual: list[str] = []
        self.cleared: list[str] = []
        self.edits: list[tuple[str, dict]] = []
        self.inserted: list[EventDraft] = []
        self.rules: dict[tuple[str, datetime.date], str] = {}
        self.guest = 0

    def add_event(self, uid="u1", **over) -> None:
        p = event_payload(**over)
        self.events[uid] = p
        for i in range(p["slot_count"]):
            self.holders.setdefault((uid, i), None)

    def add_dj(self, did: int, name: str) -> None:
        self.djs[did] = {"discord_user_id": did, "dj_name": name}

    def event_info(self, uid: str) -> EventInfo | None:
        p = self.events.get(uid)
        if p is None:
            return None
        slots = tuple(
            SlotInfo(
                page_id=f"{uid}:{n}", number=n,
                dj_page_id=str(h) if (h := self.holders.get((uid, n - 1))) else None,
                dj_name=self.djs[h]["dj_name"] if h in self.djs else None, discord_id=h,
            )
            for n in range(1, p["slot_count"] + 1)
        )
        h, m = p["start"].split(":")
        return EventInfo(
            page_id=uid, event_id=p["event_id"], date=datetime.date.fromisoformat(p["date"]),
            status=p["status"], genre=p["genre"], notes=p["notes"], slot_count=p["slot_count"],
            location=p["location"],
            start=datetime.time(int(h), int(m)), slot_minutes=p["slot_minutes"], slots=slots,
        )

    # events
    async def get_event(self, db, uid):  # noqa: ANN001, ANN201
        return self.event_info(uid)

    async def insert_event(self, db, draft: EventDraft, uid=None):  # noqa: ANN001, ANN201
        if any(p["event_id"] == draft.event_id for p in self.events.values()):
            raise DuplicateEventError(draft.event_id)
        uid = uid or f"new-{len(self.events) + 1}"
        self.add_event(
            uid, event_id=draft.event_id, date=draft.date.isoformat(),
            start=draft.start.strftime("%H:%M"), slot_minutes=draft.slot_minutes,
            slot_count=draft.slot_count, status=draft.status, genre=draft.genre,
            notes=draft.notes, location=draft.location,
        )
        self.inserted.append(draft)
        return self.event_info(uid)

    async def edit_event(self, db, uid, **fields):  # noqa: ANN001, ANN201
        p = self.events[uid]
        booked = tuple(i for (u, i), h in sorted(self.holders.items()) if u == uid and h)
        new_count = fields.get("slot_count", p["slot_count"])
        if booked and max(booked) >= new_count:
            raise SlotsBookedError(booked)
        self.edits.append((uid, fields))
        for k, v in fields.items():
            if k == "date":
                v = v.isoformat()
            if k == "start":
                v = v.strftime("%H:%M")
            p[k] = v
        return True

    # bookings
    async def get_slot_lock_holder(self, db, uid, index):  # noqa: ANN001, ANN201
        return self.holders.get((uid, index))

    async def acquire_slot_lock(self, db, uid, index, did):  # noqa: ANN001, ANN201
        if self.holders.get((uid, index)) is not None:
            return False
        self.holders[(uid, index)] = did
        return True

    async def release_slot_lock(self, db, uid, index, expected_holder=None):  # noqa: ANN001, ANN201
        held = self.holders.get((uid, index))
        if held is None or (expected_holder is not None and held != expected_holder):
            return False
        self.holders[(uid, index)] = None
        return True

    # djs
    async def get_dj_row(self, db, did):  # noqa: ANN001, ANN201
        return self.djs.get(did)

    async def dj_name_exists(self, db, name, exclude_user_id=None):  # noqa: ANN001, ANN201
        wanted = " ".join(name.split()).lower()
        return any(
            " ".join(r["dj_name"].split()).lower() == wanted and did != exclude_user_id
            for did, r in self.djs.items()
        )

    async def next_guest_id(self, db):  # noqa: ANN001, ANN201
        self.guest -= 1
        return self.guest

    async def insert_dj(self, db, *, discord_user_id, dj_name, **rest):  # noqa: ANN001, ANN201
        self.djs[discord_user_id] = {"discord_user_id": discord_user_id, "dj_name": dj_name, **rest}

    async def update_dj_fields(self, db, did, **fields):  # noqa: ANN001, ANN201
        self.djs[did].update(fields)
        return True

    # mirror and the rest
    async def set_synced(self, db, entity, key, page_id, payload):  # noqa: ANN001, ANN201
        self.synced[(entity, key)] = (page_id, payload)

    async def mark_manual(self, db, uid):  # noqa: ANN001, ANN201
        self.manual.append(uid)

    async def get_manual_page_ids(self, db, uids):  # noqa: ANN001, ANN201
        return {u for u in uids if u in self.manual}

    async def resolver(self, db):  # noqa: ANN001, ANN201
        return lambda field, day: self.rules.get((field, day))

    async def clear_announced(self, db, uid, before=None):  # noqa: ANN001, ANN201
        self.cleared.append(uid)

    def record_changed(self, ctx, entity, key, op="upsert", notion_page_id=None):  # noqa: ANN001, ANN201
        self.changed.append((entity, key))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    s = Store()
    for name in (
        "get_event", "insert_event", "edit_event", "get_slot_lock_holder",
        "acquire_slot_lock", "release_slot_lock", "get_dj_row", "dj_name_exists",
        "next_guest_id", "insert_dj", "update_dj_fields", "mark_manual", "clear_announced",
    ):
        monkeypatch.setattr(intake_mod, name, getattr(s, name))
    monkeypatch.setattr(intake_mod, "get_manual_page_ids", s.get_manual_page_ids)
    monkeypatch.setattr(intake_mod, "_resolver", s.resolver)
    monkeypatch.setattr(intake_mod.mirror, "set_synced", s.set_synced)
    monkeypatch.setattr(intake_mod, "changed", s.record_changed)
    monkeypatch.setattr(intake_mod, "local_today", lambda: TODAY)
    return s


@pytest.fixture
def env(store: Store, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    dm = AsyncMock(return_value=True)
    recheck = AsyncMock()
    monkeypatch.setattr(intake_mod, "dm_user", dm)
    monkeypatch.setattr(intake_mod, "recheck_fully_booked", recheck)
    log = MagicMock()
    for name in ("log_booked", "log_changed_in_notion", "log_notion_applied"):
        setattr(log, name, AsyncMock())
    ctx = SimpleNamespace(db=object(), log_service=log, lineup_service=MagicMock(), mirror=None)
    bot = object()
    return SimpleNamespace(
        store=store, dm=dm, recheck=recheck, log=log, ctx=ctx, bot=bot, intake=Intake(ctx, bot)
    )


def slot(uid="u1", number=1, did=None):
    return {"event_uid": uid, "number": number, "discord_id": did}


# --- events ---


async def test_apply_event_field_change(env) -> None:
    env.store.add_event("u1")
    notion = event_payload(genre="Techno", slot_count=5)
    change = Change("apply", "event", "u1", event_payload(), notion, "page-e1")
    assert await env.intake.apply(change) == "applied"
    assert env.store.edits == [("u1", {"slot_count": 5, "genre": "Techno", "genre_by_hand": True})]
    assert env.store.synced[("event", "u1")] == ("page-e1", notion)
    # added slots are left to the sync cycle's slot pass
    assert not any(entity == "slot" for entity, _ in env.store.changed)
    await env.intake.flush_log()
    env.log.log_notion_applied.assert_awaited_once()
    text = env.log.log_notion_applied.await_args.args[0]
    assert "Rave Night" in text and "genre" in text


async def test_notion_location_edit_sets_hand_marker(env) -> None:
    env.store.add_event("u1")
    notion = event_payload(location="Club Nebula", genre="Techno")
    change = Change("apply", "event", "u1", event_payload(), notion, "page-e1")
    assert await env.intake.apply(change) == "applied"
    (_, fields), = env.store.edits
    assert fields == {
        "genre": "Techno", "genre_by_hand": True,
        "location": "Club Nebula", "location_by_hand": True,
    }
    await env.intake.flush_log()
    assert "location" in env.log.log_notion_applied.await_args.args[0]


async def test_notion_genre_cleared_falls_back_to_rule(env) -> None:
    env.store.add_event("u1", genre="Hand")
    env.store.rules[("genre", datetime.date.fromisoformat(DAY))] = "House"
    change = Change(
        "apply", "event", "u1", event_payload(genre="Hand"), event_payload(genre=None), "pe"
    )
    assert await env.intake.apply(change) == "applied"
    assert env.store.edits == [("u1", {"genre": "House", "genre_by_hand": False})]


async def test_notion_genre_cleared_on_manual_event_empties_it(env) -> None:
    env.store.add_event("u1", genre="Hand")
    env.store.manual.append("u1")
    env.store.rules[("genre", datetime.date.fromisoformat(DAY))] = "House"
    change = Change(
        "apply", "event", "u1", event_payload(genre="Hand"), event_payload(genre=None), "pe"
    )
    assert await env.intake.apply(change) == "applied"
    assert env.store.edits == [("u1", {"genre": None, "genre_by_hand": False})]


async def test_new_notion_event_values_are_hand_set(env) -> None:
    notion = event_payload(genre="Techno", location="Club")
    change = Change("new_in_notion", "event", None, None, notion, "page-new")
    assert await env.intake.apply(change) == "applied"
    (draft,) = env.store.inserted
    assert (draft.genre_by_hand, draft.location_by_hand) == (True, True)


async def test_slot_count_shrink_queues_removed_slots(env) -> None:
    env.store.add_event("u1", slot_count=5)
    old = event_payload(slot_count=5)
    change = Change("apply", "event", "u1", old, event_payload(slot_count=3), "pe")
    assert await env.intake.apply(change) == "applied"
    assert env.store.changed == [("slot", "u1:3"), ("slot", "u1:4")]


async def test_reject_slot_count_below_booking(env) -> None:
    env.store.add_event("u1")
    env.store.add_dj(A, "DJ A")
    env.store.holders[("u1", 2)] = A
    change = Change("apply", "event", "u1", event_payload(), event_payload(slot_count=2), "pe")
    assert await env.intake.apply(change) == "rejected"
    assert env.intake.last_reasons and "3" in env.intake.last_reasons[0]
    assert env.store.events["u1"]["slot_count"] == 3
    assert ("event", "u1") not in env.store.synced


async def test_reject_event_moved_into_the_past(env) -> None:
    env.store.add_event("u1")
    change = Change("apply", "event", "u1", event_payload(), event_payload(date="2026-11-01"), "p")
    assert await env.intake.apply(change) == "rejected"
    assert "date" in env.intake.last_reasons[0].lower()


async def test_past_event_other_field_still_applies(env) -> None:
    env.store.add_event("u1", date="2026-11-01")
    old = event_payload(date="2026-11-01")
    change = Change("apply", "event", "u1", old, {**old, "notes": "great night"}, "p")
    assert await env.intake.apply(change) == "applied"
    assert env.store.events["u1"]["notes"] == "great night"


async def test_new_event_gets_uid_and_bot_id_pushed(env) -> None:
    notion = event_payload(event_id="2026-12-05 Special", date="2026-12-05", slot_count=2)
    change = Change("new_in_notion", "event", None, None, notion, "page-new")
    assert await env.intake.apply(change) == "applied"
    (uid,) = env.store.events
    assert env.store.synced[("event", uid)] == ("page-new", notion)
    assert ("event", uid) in env.store.changed
    assert not any(entity == "slot" for entity, _ in env.store.changed)
    assert env.store.manual == [uid]


async def test_new_event_duplicate_id_rejected(env) -> None:
    env.store.add_event("u1")
    change = Change("new_in_notion", "event", None, None, event_payload(), "page-new")
    assert await env.intake.apply(change) == "rejected"
    assert "already" in env.intake.last_reasons[0]


# --- DJs ---


async def test_new_dj_duplicate_name_rejected(env) -> None:
    env.store.add_dj(B, "DJ New")
    change = Change("new_in_notion", "dj", None, None, dj_payload(" dj  new "), "page-dj")
    assert await env.intake.apply(change) == "rejected"
    assert "already" in env.intake.last_reasons[0]
    assert A not in env.store.djs


async def test_new_dj_with_discord_id(env) -> None:
    notion = dj_payload()
    change = Change("new_in_notion", "dj", None, None, notion, "page-dj")
    assert await env.intake.apply(change) == "applied"
    assert env.store.djs[A]["dj_name"] == "DJ New"
    assert env.store.djs[A]["active_booking"] is True
    assert env.store.synced[("dj", str(A))] == ("page-dj", notion)
    assert ("dj", str(A)) in env.store.changed


async def test_new_guest_dj(env) -> None:
    notion = dj_payload("Guest Star", did=-7)
    change = Change("new_in_notion", "dj", None, None, notion, "page-g")
    assert await env.intake.apply(change) == "applied"
    assert env.store.djs[-1]["dj_name"] == "Guest Star"
    assert env.store.djs[-1]["active_booking"] is False
    assert env.store.synced[("dj", "-1")] == ("page-g", notion)
    assert ("dj", "-1") in env.store.changed


async def test_new_dj_bad_link_rejected(env) -> None:
    change = Change("new_in_notion", "dj", None, None, dj_payload(link="not a link"), "p")
    assert await env.intake.apply(change) == "rejected"
    assert "link" in env.intake.last_reasons[0]


async def test_dj_rename_applies(env) -> None:
    env.store.add_dj(A, "DJ Old")
    old = dj_payload("DJ Old")
    change = Change("apply", "dj", str(A), old, dj_payload("DJ Renamed"), "page-dj")
    assert await env.intake.apply(change) == "applied"
    assert env.store.djs[A]["dj_name"] == "DJ Renamed"


# --- slots ---


async def test_slot_set_books_and_dms(env) -> None:
    env.store.add_event("u1")
    env.store.add_dj(A, "DJ A")
    notion = slot(number=2, did=A)
    change = Change("apply", "slot", "u1:1", slot(number=2), notion, "page-s")
    assert await env.intake.apply(change) == "applied"
    assert env.store.holders[("u1", 1)] == A
    env.dm.assert_awaited_once()
    assert env.dm.await_args.args[1] == A and "lineup" in env.dm.await_args.args[2]
    env.ctx.lineup_service.notify_changed.assert_called_once()
    env.recheck.assert_awaited_once()
    env.log.log_booked.assert_awaited_once()
    assert env.log.log_booked.await_args.args[0] == "DJ A"
    assert env.log.log_booked.await_args.args[2:] == (1, "Notion")
    assert env.store.synced[("slot", "u1:1")] == ("page-s", notion)


async def test_slot_of_past_event_rejected(env) -> None:
    """I3: slots of past events are never changed from Notion."""
    env.store.add_event("u1", date="2026-11-01")
    env.store.holders[("u1", 0)] = A  # DJ A was deleted; the past booking stays
    change = Change("apply", "slot", "u1:0", slot(did=A), slot(), "page-s")
    assert await env.intake.apply(change) == "rejected"
    assert "past" in env.intake.last_reasons[0]
    assert env.store.holders[("u1", 0)] == A
    env.dm.assert_not_awaited()
    assert ("slot", "u1:0") not in env.store.synced


async def test_slot_cleared_frees_and_dms(env) -> None:
    env.store.add_event("u1")
    env.store.add_dj(A, "DJ A")
    env.store.holders[("u1", 0)] = A
    change = Change("apply", "slot", "u1:0", slot(did=A), slot(), "page-s")
    assert await env.intake.apply(change) == "applied"
    assert env.store.holders[("u1", 0)] is None
    assert env.dm.await_args.args[1] == A and "removed" in env.dm.await_args.args[2]
    assert env.store.cleared == ["u1"]
    env.log.log_changed_in_notion.assert_awaited_once()
    env.ctx.lineup_service.notify_changed.assert_called_once()


async def test_slot_replaced_does_both(env) -> None:
    env.store.add_event("u1")
    env.store.add_dj(A, "DJ A")
    env.store.add_dj(B, "DJ B")
    env.store.holders[("u1", 0)] = A
    change = Change("apply", "slot", "u1:0", slot(did=A), slot(did=B), "page-s")
    assert await env.intake.apply(change) == "applied"
    assert env.store.holders[("u1", 0)] == B
    assert [c.args[1] for c in env.dm.await_args_list] == [A, B]
    env.log.log_changed_in_notion.assert_awaited_once()
    env.log.log_booked.assert_awaited_once()


async def test_slot_set_unknown_dj_rejected(env) -> None:
    env.store.add_event("u1")
    change = Change("apply", "slot", "u1:0", slot(), slot(did=A), "page-s")
    assert await env.intake.apply(change) == "rejected"
    assert env.intake.last_reasons
    assert env.store.holders[("u1", 0)] is None
    env.dm.assert_not_awaited()


async def test_slot_held_by_someone_else_rejected(env) -> None:
    env.store.add_event("u1")
    env.store.add_dj(A, "DJ A")
    env.store.add_dj(B, "DJ B")
    env.store.holders[("u1", 0)] = B  # booked in the bot since the last sync
    change = Change("apply", "slot", "u1:0", slot(), slot(did=A), "page-s")
    assert await env.intake.apply(change) == "rejected"
    assert env.store.holders[("u1", 0)] == B


async def test_new_slot_page_outside_slot_count_rejected(env) -> None:
    env.store.add_event("u1")
    env.store.add_dj(A, "DJ A")
    change = Change("new_in_notion", "slot", None, None, slot(number=9, did=A), "page-s")
    assert await env.intake.apply(change) == "rejected"


async def test_new_slot_page_books(env) -> None:
    env.store.add_event("u1")
    env.store.add_dj(A, "DJ A")
    notion = slot(number=3, did=A)
    change = Change("new_in_notion", "slot", None, None, notion, "page-s")
    assert await env.intake.apply(change) == "applied"
    assert env.store.holders[("u1", 2)] == A
    assert env.store.synced[("slot", "u1:2")] == ("page-s", notion)


# --- log batching and robustness ---


async def test_flush_log_batches_ten_lines_and_more(env) -> None:
    env.intake._applied = [f"line {i}" for i in range(13)]
    await env.intake.flush_log()
    text = env.log.log_notion_applied.await_args.args[0]
    assert "line 9" in text and "line 10" not in text and "and 3 more" in text
    env.log.log_notion_applied.reset_mock()
    await env.intake.flush_log()
    env.log.log_notion_applied.assert_not_awaited()


async def test_apply_never_raises(env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.store.add_event("u1")
    monkeypatch.setattr(intake_mod, "edit_event", AsyncMock(side_effect=RuntimeError("db down")))
    change = Change("apply", "event", "u1", event_payload(), event_payload(genre="x"), "p")
    assert await env.intake.apply(change) == "rejected"
    assert env.intake.last_reasons
