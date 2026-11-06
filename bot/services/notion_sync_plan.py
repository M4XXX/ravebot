"""Pure planner that compares Notion slot rows with Postgres slot locks.

plan_sync() reads typed Notion events and the slot_locks rows and returns the
actions a sync service must apply. It performs no I/O.

Keys: a lock is keyed by (event.page_id, slot_index); slot_index is 0-based and
the Notion slot number is slot_index + 1.
"""
import datetime
from dataclasses import dataclass
from typing import Literal

from bot.db.bookings import SlotLock
from bot.notion.models import EventInfo, SlotInfo

# A lock younger than this is a booking in flight: the bot may have taken the
# Postgres lock but not yet written Notion, so an empty Notion row is not a removal.
FREE_GRACE = datetime.timedelta(seconds=120)

SyncKind = Literal["adopt", "free", "revert", "create_slots", "trim_slots", "skip"]


@dataclass(frozen=True)
class SyncAction:
    """One step for the sync service to apply.

    kinds:
      adopt         Notion row holds exactly one DJ with a Discord ID and Postgres has
                    no holder. Lock slot_index for ``discord_id`` (hand entry; DM + log).
      free          Notion row is empty, Postgres holder ``discord_id`` booked more than
                    120 s ago (or booked_at unknown). Release the lock (hand removal;
                    DM + log).
      revert        Rewrite the Notion row's DJ relation; never touches Postgres.
                    - ``discord_id`` set: the Postgres holder. Write exactly that DJ
                      (look up the holder's DJ page) into the row, replacing whatever
                      Notion holds (another DJ and/or extra DJs). Log it.
                    - ``discord_id`` None: the "extras" case. The row holds more than one
                      DJ and Postgres has no holder. Keep the first DJ
                      (``row.dj_page_id`` of ``event.slots`` entry with number
                      slot_index + 1) and remove the extras (write the relation
                      ``[first DJ]``). No lock is taken in this cycle; if the first DJ
                      has a Discord ID the next cycle sees one DJ without a holder and
                      emits ``adopt``. Log it.
      create_slots  Event-level (slot_index None). Create empty Slots rows for
                    ``numbers``. Locks of those indexes are reconciled next cycle.
      trim_slots    Event-level (slot_index None). Trash the rows with ``numbers``
                    (all above slot_count, no DJ, no Postgres holder).
      skip          A row above slot_count that holds a DJ or a Postgres holder; it is
                    not trimmed. slot_index = number - 1, ``numbers`` = (number,).
                    Log only.

    ``detail`` is a human-readable English reason for the log.
    """

    kind: SyncKind
    event: EventInfo
    slot_index: int | None
    detail: str
    discord_id: int | None = None
    numbers: tuple[int, ...] = ()


def _holder(
    locks: dict[tuple[str, int], SlotLock], event: EventInfo, index: int
) -> SlotLock | None:
    lock = locks.get((event.page_id, index))
    if lock is None or lock.discord_user_id is None:
        return None
    return lock


def _rows_by_number(event: EventInfo) -> dict[int, SlotInfo]:
    """First row per number; duplicates resolved by page_id so input order does not matter."""
    rows: dict[int, SlotInfo] = {}
    for row in sorted(event.slots, key=lambda r: (r.number, r.page_id)):
        rows.setdefault(row.number, row)
    return rows


def _slot_action(
    event: EventInfo, index: int, row: SlotInfo, lock: SlotLock | None, now: datetime.datetime
) -> SyncAction | None:
    label = f"{event.event_id} #{index + 1}"
    holder = lock.discord_user_id if lock else None
    if row.extra_dj_page_ids:
        if holder is not None:
            return SyncAction(
                "revert", event, index,
                f"{label}: several DJs in Notion, restoring Postgres holder {holder}",
                discord_id=holder,
            )
        return SyncAction(
            "revert", event, index,
            f"{label}: several DJs in Notion and no holder, keeping the first DJ "
            f"{row.dj_name or row.dj_page_id} and clearing the extras",
        )
    if row.dj_page_id is None:
        if lock is None or holder is None:
            return None
        if lock.booked_at is not None and now - lock.booked_at <= FREE_GRACE:
            return None
        return SyncAction(
            "free", event, index,
            f"{label}: DJ removed in Notion, releasing lock of {holder}",
            discord_id=holder,
        )
    if row.discord_id is None:
        return None  # guest DJ: Notion only, no lock, no DM
    if holder is None:
        return SyncAction(
            "adopt", event, index,
            f"{label}: DJ {row.dj_name or row.dj_page_id} entered in Notion, adopting",
            discord_id=row.discord_id,
        )
    if holder == row.discord_id:
        return None
    return SyncAction(
        "revert", event, index,
        f"{label}: Notion shows {row.dj_name or row.dj_page_id} ({row.discord_id}) but "
        f"Postgres holder is {holder}, restoring the holder",
        discord_id=holder,
    )


def _plan_event(
    event: EventInfo, locks: dict[tuple[str, int], SlotLock], now: datetime.datetime
) -> list[SyncAction]:
    rows = _rows_by_number(event)
    actions: list[SyncAction] = []

    missing = tuple(n for n in range(1, event.slot_count + 1) if n not in rows)
    if missing:
        actions.append(SyncAction(
            "create_slots", event, None,
            f"{event.event_id}: creating missing slot rows {', '.join(f'#{n}' for n in missing)}",
            numbers=missing,
        ))

    for index in range(event.slot_count):
        row = rows.get(index + 1)
        if row is None:
            continue
        action = _slot_action(event, index, row, _holder(locks, event, index), now)
        if action is not None:
            actions.append(action)

    trim: list[int] = []
    for number in sorted(n for n in rows if n > event.slot_count):
        row = rows[number]
        lock = _holder(locks, event, number - 1)
        if row.dj_page_id is None and not row.extra_dj_page_ids and lock is None:
            trim.append(number)
            continue
        holder = f"Postgres holder {lock.discord_user_id}" if lock else "?"
        who = row.dj_name or row.dj_page_id or holder
        actions.append(SyncAction(
            "skip", event, number - 1,
            f"{event.event_id} #{number}: above the event's {event.slot_count} slots but "
            f"held by {who}, not removed",
            numbers=(number,),
        ))
    if trim:
        actions.append(SyncAction(
            "trim_slots", event, None,
            f"{event.event_id}: removing empty slot rows above {event.slot_count}: "
            f"{', '.join(f'#{n}' for n in trim)}",
            numbers=tuple(trim),
        ))
    return actions


def plan_sync(
    events: list[EventInfo],
    locks: dict[tuple[str, int], SlotLock],
    now: datetime.datetime,
) -> list[SyncAction]:
    """Return the sync actions, ordered by event (date, start, ID) then slot.

    Within one event: create_slots first, then slot actions by index, then skips
    above slot_count by number, then trim_slots. Locks of events not in ``events``
    and of indexes outside 0..slot_count-1 without a row are ignored.
    """
    ordered = sorted(events, key=lambda e: (e.date, e.start, e.event_id, e.page_id))
    actions: list[SyncAction] = []
    for event in ordered:
        actions.extend(_plan_event(event, locks, now))
    return actions
