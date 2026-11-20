"""Events repository: the bot's own events (Postgres is the source of truth).

An event's key is ``events.uid``; ``EventInfo.page_id`` carries it. Slots live in
``slot_locks`` (rows 0..slot_count-1), shown as slot numbers 1..slot_count.
Functions accept a Pool or a Connection; ``insert_event`` opens its own
transaction only when given a Pool.
"""
import dataclasses
import datetime
import uuid
from dataclasses import dataclass

import asyncpg

from bot.db.bookings import init_event_slots
from bot.notion.models import EventInfo, SlotInfo
from bot.timezone import local_today

__all__ = [
    "DuplicateEventError",
    "EventDraft",
    "event_id_exists",
    "get_event",
    "insert_event",
    "list_events",
    "set_event_status",
    "update_event",
]

Executor = asyncpg.Pool | asyncpg.Connection


class DuplicateEventError(Exception):
    """An event with this ID already exists."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"Event ID already exists: {event_id}")
        self.event_id = event_id


@dataclass(frozen=True)
class EventDraft:
    event_id: str
    date: datetime.date
    start: datetime.time
    slot_minutes: int
    slot_count: int
    status: str | None = None
    genre: str | None = None
    notes: str | None = None


_FIELDS = tuple(f.name for f in dataclasses.fields(EventDraft))
_COLUMNS = "uid, event_id, date, start, slot_minutes, slot_count, status, genre, notes"


def _parse_start(value: str) -> datetime.time:
    hour, minute = value.split(":")
    return datetime.time(int(hour), int(minute))


def _format_start(value: datetime.time) -> str:
    return value.strftime("%H:%M")


def _empty_slots(uid: str, slot_count: int) -> tuple[SlotInfo, ...]:
    return tuple(
        SlotInfo(
            page_id=f"{uid}:{n}", number=n, dj_page_id=None, dj_name=None, discord_id=None
        )
        for n in range(1, slot_count + 1)
    )


def _event(row: asyncpg.Record, slots: tuple[SlotInfo, ...]) -> EventInfo:
    return EventInfo(
        page_id=row["uid"],
        event_id=row["event_id"],
        date=row["date"],
        status=row["status"],
        genre=row["genre"],
        notes=row["notes"],
        slot_count=row["slot_count"],
        start=_parse_start(row["start"]),
        slot_minutes=row["slot_minutes"],
        slots=slots,
    )


async def _slots_by_event(
    db: Executor, event_ids: list[str]
) -> dict[str, list[tuple[str, SlotInfo]]]:
    """Slots per bot event_id (not uid), one query for all events."""
    rows = await db.fetch(
        """
        SELECT e.uid, s.slot_index, s.discord_user_id, d.dj_name
        FROM events e
        JOIN slot_locks s ON s.event_id = e.event_id AND s.slot_index < e.slot_count
        LEFT JOIN djs d ON d.discord_user_id = s.discord_user_id
        WHERE e.event_id = ANY($1::text[])
        ORDER BY e.uid, s.slot_index
        """,
        event_ids,
    )
    out: dict[str, list[tuple[str, SlotInfo]]] = {}
    for r in rows:
        holder = r["discord_user_id"]
        number = r["slot_index"] + 1
        out.setdefault(r["uid"], []).append(
            (
                r["uid"],
                SlotInfo(
                    page_id=f"{r['uid']}:{number}",
                    number=number,
                    dj_page_id=str(holder) if holder is not None else None,
                    dj_name=r["dj_name"],
                    discord_id=holder,
                ),
            )
        )
    return out


async def _hydrate(db: Executor, rows: list[asyncpg.Record]) -> list[EventInfo]:
    if not rows:
        return []
    by_uid = await _slots_by_event(db, [r["event_id"] for r in rows])
    return [
        _event(r, tuple(s for _, s in by_uid.get(r["uid"], ()))) for r in rows
    ]


async def list_events(db: Executor, *, include_past: bool = False) -> list[EventInfo]:
    """Events with slots joined, sorted by (date, start); past ones only on request."""
    rows = await db.fetch(
        f"SELECT {_COLUMNS} FROM events WHERE ($1::bool OR date >= $2) "
        "ORDER BY date, start, uid",
        include_past,
        local_today(),
    )
    return await _hydrate(db, rows)


async def get_event(db: Executor, uid: str) -> EventInfo | None:
    row = await db.fetchrow(f"SELECT {_COLUMNS} FROM events WHERE uid = $1", uid)
    if row is None:
        return None
    return (await _hydrate(db, [row]))[0]


async def _insert(db: asyncpg.Connection, draft: EventDraft, uid: str) -> None:
    try:
        await db.execute(
            """
            INSERT INTO events
                (uid, event_id, date, start, slot_minutes, slot_count, status, genre, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uid,
            draft.event_id,
            draft.date,
            _format_start(draft.start),
            draft.slot_minutes,
            draft.slot_count,
            draft.status,
            draft.genre,
            draft.notes,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateEventError(draft.event_id) from exc
    await init_event_slots(db, draft.event_id, draft.slot_count)


async def insert_event(
    db: Executor, draft: EventDraft, uid: str | None = None
) -> EventInfo:
    """Insert the event and its slot rows; DuplicateEventError on a taken event_id."""
    uid = uid or str(uuid.uuid4())
    if isinstance(db, asyncpg.Pool):
        async with db.acquire() as conn:
            async with conn.transaction():
                await _insert(conn, draft, uid)
    else:
        await _insert(db, draft, uid)
    return EventInfo(
        page_id=uid,
        event_id=draft.event_id,
        date=draft.date,
        status=draft.status,
        genre=draft.genre,
        notes=draft.notes,
        slot_count=draft.slot_count,
        start=draft.start,
        slot_minutes=draft.slot_minutes,
        slots=_empty_slots(uid, draft.slot_count),
    )


async def update_event(db: Executor, uid: str, **fields: object) -> bool:
    """Update the given EventDraft fields; True when the event exists."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"Unknown event field(s): {', '.join(sorted(unknown))}")
    if not fields:
        return await db.fetchval("SELECT EXISTS (SELECT 1 FROM events WHERE uid = $1)", uid)
    sets = []
    values: list[object] = [uid]
    for name, value in fields.items():
        values.append(_format_start(value) if name == "start" else value)  # type: ignore[arg-type]
        sets.append(f"{name} = ${len(values)}")
    result = await db.execute(
        f"UPDATE events SET {', '.join(sets)}, updated_at = now() WHERE uid = $1", *values
    )
    return result.endswith(" 1")


async def set_event_status(db: Executor, uid: str, status: str | None) -> None:
    await db.execute(
        "UPDATE events SET status = $2, updated_at = now() WHERE uid = $1", uid, status
    )


async def event_id_exists(db: Executor, event_id: str) -> bool:
    return bool(
        await db.fetchval("SELECT EXISTS (SELECT 1 FROM events WHERE event_id = $1)", event_id)
    )
