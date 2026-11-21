"""Schedule rules, default values and per-event genre/location/note.

Postgres only: rule writes are mirrored by the Bot-data poll, event writes are queued
with `changed(ctx, "event", uid)` after commit. Nothing here awaits Notion.
Rules and defaults apply to scheduled events only (never to manual events), and never
to a field set by hand (`<field>_by_hand`).
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bot.db import events as db_events
from bot.db import schedule_rules as db_rules
from bot.db.config import get_value
from bot.db.manual_events import get_manual_page_ids
from bot.notion.models import FINISHED_STATUSES, EventInfo
from bot.services import event_replenish
from bot.services.schedule_rules import Field, Rule, conflicts, reanchor, resolve
from bot.sync.worker import changed
from bot.timezone import local_today
from bot.ui import texts

if TYPE_CHECKING:
    import asyncpg

    from bot.context import BotContext

__all__ = [
    "UNSET",
    "PendingChange",
    "RuleConflict",
    "ScheduleDate",
    "ScheduleService",
    "load_resolver",
    "recompute_hand_markers",
    "resolver_without_bot",
    "schedule_guild_id",
]

logger = logging.getLogger(__name__)

# Leaves a field of set_event_values unchanged.
UNSET: Any = object()

FIELDS: tuple[Field, ...] = ("genre", "location")
_MIN_EVERY, _MAX_EVERY = 1, 52
# Rule start dates are picked from at most this many upcoming dates (select menu limit).
_ANCHOR_WINDOW = 25
# Limit, too-long text, one-line text (None: newlines allowed).
_LIMITS: dict[str, tuple[int, str, str | None]] = {
    "genre": (40, texts.GENRE_TOO_LONG, texts.GENRE_ONE_LINE),
    "location": (80, texts.LOCATION_TOO_LONG, texts.LOCATION_ONE_LINE),
    "note": (300, texts.NOTE_TOO_LONG, None),
}

Resolver = Callable[[Field, datetime.date], "str | None"]


@dataclass(frozen=True)
class ScheduleDate:
    date: datetime.date
    event: EventInfo | None  # None: projected, not created yet

    @property
    def label(self) -> str:
        name = texts.event_name(self.event) if self.event else texts.PROJECTED_EVENT_NAME
        return texts.schedule_date_label(name, self.date)


@dataclass(frozen=True)
class PendingChange:
    event: EventInfo
    field: Field
    old: str | None
    new: str | None


class RuleConflict(Exception):
    """The rule would hit a schedule date another rule for the same field hits."""

    def __init__(self, rules: list[Rule]) -> None:
        super().__init__(f"Conflicting rules: {[r.id for r in rules]}")
        self.rules = rules


def clean_value(kind: str, value: str | None) -> str | None:
    """Stripped value, None when empty; ValueError (text from texts) past the limits."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    limit, too_long, one_line = _LIMITS[kind]
    if one_line is not None and ("\n" in value or "\r" in value):
        raise ValueError(one_line)
    if len(value) > limit:
        raise ValueError(too_long)
    return value


async def _interval_days(db: asyncpg.Pool | asyncpg.Connection, guild_id: int) -> int:
    raw = await get_value(db, guild_id, "event_interval_days")
    return max(1, event_replenish._to_int(raw, 7))


async def load_resolver(
    db: asyncpg.Pool | asyncpg.Connection, guild_id: int, interval_days: int
) -> Resolver:
    """Read rules and defaults once; the returned function resolves a field for a date."""
    rules = await db_rules.list_rules(db, guild_id)
    defaults = {f: await db_rules.get_default(db, guild_id, f) for f in FIELDS}

    def resolver(field: Field, day: datetime.date) -> str | None:
        return resolve(field, day, rules, defaults[field], interval_days)

    return resolver


async def schedule_guild_id(db: asyncpg.Pool | asyncpg.Connection) -> int | None:
    """The guild holding rules or defaults (lowest if several); None without any."""
    return await db.fetchval(
        "SELECT min(guild_id) FROM ("
        " SELECT guild_id FROM schedule_rules"
        " UNION SELECT guild_id FROM config"
        "  WHERE key = ANY($1::text[]) AND coalesce(value, '') <> ''"
        ") AS g",
        [db_rules.DEFAULT_GENRE_KEY, db_rules.DEFAULT_LOCATION_KEY],
    )


async def resolver_without_bot(db: asyncpg.Pool | asyncpg.Connection) -> Resolver:
    """A resolver for code that has no bot (Notion intake, import): the guild holding the
    rules and defaults. Without any, every field resolves to None."""
    guild_id = await schedule_guild_id(db)
    if guild_id is None:
        return lambda field, day: None
    return await load_resolver(db, guild_id, await _interval_days(db, guild_id))


async def recompute_hand_markers(conn: asyncpg.Connection, guild_id: int | None) -> None:
    """Set genre_by_hand/location_by_hand from the stored values (after an import).

    Scheduled event: by hand when the value differs from what the rules resolve for its
    date. Manual event: by hand when it has a value. Without rules or defaults
    (guild_id None) every value counts as by hand.
    """
    rows = await conn.fetch("SELECT uid, date, genre, location FROM events")
    manual = await get_manual_page_ids(conn, [r["uid"] for r in rows])
    if guild_id is None:
        resolver: Resolver = lambda field, day: None  # noqa: E731
    else:
        resolver = await load_resolver(conn, guild_id, await _interval_days(conn, guild_id))
    for row in rows:
        marks = {}
        for field in FIELDS:
            value = row[field]
            if not value:
                marks[field] = False
            elif row["uid"] in manual:
                marks[field] = True
            else:
                marks[field] = value != resolver(field, row["date"])
        await conn.execute(
            "UPDATE events SET genre_by_hand = $2, location_by_hand = $3 WHERE uid = $1",
            row["uid"], marks["genre"], marks["location"],
        )


class ScheduleService:
    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    @property
    def _db(self) -> asyncpg.Pool:
        db = self.ctx.db
        assert db is not None
        return db

    async def interval_days(self, guild_id: int) -> int:
        return await _interval_days(self._db, guild_id)

    # -- schedule dates ---------------------------------------------------------------

    async def _events(self) -> tuple[list[EventInfo], list[EventInfo], set[str]]:
        """(scheduled events dated today or later, the open ones, manual event IDs)."""
        db = self._db
        today = local_today()
        events = [e for e in await db_events.list_events(db) if e.date >= today]
        manual = await get_manual_page_ids(db, [e.page_id for e in events])
        scheduled = [e for e in events if e.page_id not in manual]
        upcoming = [e for e in scheduled if e.status not in FINISHED_STATUSES]
        return scheduled, upcoming, {e.event_id for e in events if e.page_id in manual}

    async def upcoming_dates(
        self,
        guild_id: int,
        count: int = 10,
        *,
        interval_days: int | None = None,
        first_date: datetime.date | None = None,
    ) -> list[ScheduleDate]:
        """Upcoming scheduled events, then projected dates continuing the grid.

        The projection follows replenish: one interval after max(latest scheduled
        date, today), or from the first date when no scheduled event exists, skipping
        dates whose event ID a manual event holds.
        """
        db = self._db
        interval = (
            max(1, interval_days)
            if interval_days is not None
            else await self.interval_days(guild_id)
        )
        today = local_today()
        scheduled, upcoming, manual_ids = await self._events()
        out = [ScheduleDate(e.date, e) for e in upcoming]
        if scheduled:
            first = None
        elif first_date is not None:
            first = first_date if first_date >= today else None
        else:
            raw = await get_value(db, guild_id, event_replenish.FIRST_DATE_KEY)
            first = event_replenish._parse_first_date(raw, today)
        base = max([e.date for e in scheduled] + [today])
        dates: Iterable[datetime.date] = event_replenish._candidate_dates(first, base, interval)
        for day in dates:
            if len(out) >= count:
                break
            # replenish skips a date whose event ID a manual event already holds
            if event_replenish.scheduled_event_id(day) not in manual_ids:
                out.append(ScheduleDate(day, None))
        out.sort(key=lambda d: d.date)
        return out[:count]

    # -- rules and defaults -----------------------------------------------------------

    async def _validate_rule(
        self, guild_id: int, rule: Rule, keep_anchor: datetime.date | None = None
    ) -> Rule:
        value = clean_value(rule.field, rule.value)
        if value is None:
            raise ValueError(texts.RULE_VALUE_EMPTY)
        if not _MIN_EVERY <= rule.every <= _MAX_EVERY:
            raise ValueError(texts.RULE_EVERY_RANGE)
        if rule.anchor != keep_anchor:
            dates = await self.upcoming_dates(guild_id, _ANCHOR_WINDOW)
            if rule.anchor not in {d.date for d in dates}:
                raise ValueError(texts.RULE_ANCHOR_NOT_UPCOMING)
        return dataclasses.replace(rule, value=value)

    async def _check_conflicts(self, guild_id: int, rule: Rule) -> None:
        existing = await db_rules.list_rules(self._db, guild_id)
        found = conflicts(rule, existing, await self.interval_days(guild_id))
        if found:
            raise RuleConflict(found)

    async def add_rule(self, guild_id: int, rule: Rule) -> Rule:
        """Validate, check conflicts, insert. ValueError, RuleConflict."""
        rule = await self._validate_rule(guild_id, dataclasses.replace(rule, id=None))
        async with event_replenish.replenish_guard():
            await self._check_conflicts(guild_id, rule)
            return await db_rules.insert_rule(self._db, guild_id, rule)

    async def edit_rule(self, guild_id: int, rule: Rule) -> Rule:
        """Validate, check conflicts, update. LookupError, ValueError, RuleConflict.

        An unchanged anchor stays valid even when its date has passed.
        """
        assert rule.id is not None
        stored = await db_rules.get_rule(self._db, guild_id, rule.id)
        if stored is None:
            raise LookupError(rule.id)
        rule = await self._validate_rule(guild_id, rule, keep_anchor=stored.anchor)
        async with event_replenish.replenish_guard():
            await self._check_conflicts(guild_id, rule)
            if not await db_rules.update_rule(self._db, guild_id, rule):
                raise LookupError(rule.id)
        return rule

    async def remove_rule(self, guild_id: int, rule_id: int) -> bool:
        return await db_rules.delete_rule(self._db, guild_id, rule_id)

    async def set_default(self, guild_id: int, field: Field, value: str | None) -> None:
        await db_rules.set_default(self._db, guild_id, field, clean_value(field, value))

    async def resolved(self, guild_id: int, field: Field, day: datetime.date) -> str | None:
        return (await self._resolver(guild_id))(field, day)

    async def _resolver(self, guild_id: int) -> Resolver:
        return await load_resolver(self._db, guild_id, await self.interval_days(guild_id))

    # -- pending changes --------------------------------------------------------------

    async def pending_changes(self, guild_id: int) -> list[PendingChange]:
        """Upcoming scheduled events whose non-hand fields differ from the rule value."""
        resolver = await self._resolver(guild_id)
        _, upcoming, _ = await self._events()
        out = [
            PendingChange(e, field, getattr(e, field), new)
            for e in upcoming
            for field in FIELDS
            if not getattr(e, f"{field}_by_hand")
            and (new := resolver(field, e.date)) != getattr(e, field)
        ]
        out.sort(key=lambda p: (p.event.date, p.field))
        return out

    async def apply(
        self, guild_id: int, picks: Collection[tuple[str, Field]] | None
    ) -> int:
        """Write the pending values (all, or the picked ones still pending now).

        Pending changes and values are recomputed here, and each event row is locked
        and its hand markers re-read before the write. Returns the events changed.
        """
        wanted = None if picks is None else set(picks)
        by_uid: dict[str, dict[str, str | None]] = {}
        for p in await self.pending_changes(guild_id):
            if wanted is None or (p.event.page_id, p.field) in wanted:
                by_uid.setdefault(p.event.page_id, {})[p.field] = p.new
        written: list[str] = []
        for uid, values in by_uid.items():
            async with self._db.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT genre_by_hand, location_by_hand FROM events"
                        " WHERE uid = $1 FOR UPDATE",
                        uid,
                    )
                    if row is None:
                        continue
                    fields = {f: v for f, v in values.items() if not row[f"{f}_by_hand"]}
                    if not fields:
                        continue
                    await db_events.update_event(conn, uid, **fields)
            written.append(uid)
        for uid in written:
            changed(self.ctx, "event", uid)
        if written:
            await self._refresh_lineup()
        return len(written)

    # -- per-event values -------------------------------------------------------------

    async def set_event_values(
        self,
        guild_id: int,
        uid: str,
        *,
        genre: str | None = UNSET,
        location: str | None = UNSET,
        note: str | None = UNSET,
    ) -> EventInfo:
        """Set or clear an event's genre, location and note. LookupError, ValueError.

        A value marks genre/location as set by hand. Clearing a scheduled event's
        genre/location hands it back to the rules (resolved value now); on a manual
        event it empties the field.
        """
        given = {"genre": genre, "location": location, "note": note}
        values = {k: clean_value(k, v) for k, v in given.items() if v is not UNSET}
        resolver = await self._resolver(guild_id)
        async with self._db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT 1 FROM events WHERE uid = $1 FOR UPDATE", uid)
                event = await db_events.get_event(conn, uid)
                if event is None:
                    raise LookupError(uid)
                manual = uid in await get_manual_page_ids(conn, [uid])
                fields: dict[str, object] = {}
                if "note" in values:
                    fields["notes"] = values.pop("note")
                for field, value in values.items():
                    if value is not None:
                        fields[field], fields[f"{field}_by_hand"] = value, True
                    else:
                        fields[field] = None if manual else resolver(field, event.date)  # type: ignore[arg-type]
                        fields[f"{field}_by_hand"] = False
                if fields:
                    await db_events.update_event(conn, uid, **fields)
        if fields:
            changed(self.ctx, "event", uid)
            await self._refresh_lineup()
        updated = await db_events.get_event(self._db, uid)
        if updated is None:
            raise LookupError(uid)
        return updated

    async def _refresh_lineup(self) -> None:
        service = self.ctx.lineup_service
        if service is None:
            return
        try:
            await service.refresh()
        except Exception:
            logger.exception("Lineup refresh after a schedule change failed")

    # -- re-anchoring -----------------------------------------------------------------

    @staticmethod
    def _reanchored(rules: list[Rule], new_interval: int, grid_origin: datetime.date) -> list[Rule]:
        return [
            dataclasses.replace(r, anchor=reanchor(r.anchor, grid_origin, new_interval))
            for r in rules
        ]

    async def check_reanchor(
        self, guild_id: int, new_interval: int, grid_origin: datetime.date
    ) -> list[tuple[Rule, Rule]]:
        """Conflicting pairs the re-anchored rules would have (empty: safe)."""
        interval = max(1, new_interval)
        moved = self._reanchored(
            await db_rules.list_rules(self._db, guild_id), interval, grid_origin
        )
        return [
            (a, b)
            for i, a in enumerate(moved)
            for b in conflicts(a, moved[i + 1:], interval)
        ]

    async def reanchor_rules(
        self, guild_id: int, new_interval: int, grid_origin: datetime.date
    ) -> None:
        """Move every rule's anchor onto the new grid."""
        interval = max(1, new_interval)
        async with event_replenish.replenish_guard(), self._db.acquire() as conn:
            async with conn.transaction():
                rules = await db_rules.list_rules(conn, guild_id)
                for rule in self._reanchored(rules, interval, grid_origin):
                    await db_rules.update_rule(conn, guild_id, rule)

