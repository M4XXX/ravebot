"""Data service for the schedule page of the admin page (defaults, rules, event values).

A thin layer over ``ScheduleService``: it finds the guild, parses form text and turns the
service errors into the texts the Discord commands use. Nothing here awaits Notion.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any

from bot.db import config as config_db
from bot.db import events as db_events
from bot.db import schedule_rules as db_rules
from bot.results import ActionResult
from bot.services import event_replenish
from bot.services.schedule import FIELDS, RuleConflict, ScheduleDate
from bot.services.schedule_rules import Rule
from bot.ui import texts

logger = logging.getLogger(__name__)

NOT_READY = "The bot is not on a server yet, so there is no schedule."
NO_EVENT = texts.EDIT_EVENT_NOT_FOUND
SAVE_FAILED = "Could not save the change. Try again."
BAD_FIELD = "Choose genre or location."
ANCHORS = 25
SHOWN_EVENTS = 10


@dataclass(frozen=True)
class RuleView:
    rule: Rule
    label: str


@dataclass(frozen=True)
class ScheduleOverview:
    enabled: bool
    interval_days: int
    first_date: str | None
    lookahead: int
    defaults: dict[str, str]
    rules: list[RuleView]
    dates: list[ScheduleDate]  # the upcoming events shown in the table
    anchors: list[ScheduleDate]  # the dates a rule can start on
    pending: int


@dataclass(frozen=True)
class PendingView:
    uid: str
    field: str
    text: str


def pick_key(uid: str, field: str) -> str:
    return f"{uid}:{field}"


def parse_pick(raw: str) -> tuple[str, str] | None:
    uid, _, field = raw.rpartition(":")
    return (uid, field) if uid and field in FIELDS else None


class SchedulePageService:
    def __init__(self, ctx: Any, bot: Any) -> None:
        self._ctx = ctx
        self._bot = bot

    def _guild_id(self) -> int | None:
        if not self._bot.guilds or self._ctx.schedule_service is None or self._ctx.db is None:
            return None
        return self._bot.guilds[0].id

    @property
    def _service(self) -> Any:
        return self._ctx.schedule_service

    async def _labels(self, guild_id: int) -> tuple[list[ScheduleDate], dict[datetime.date, str]]:
        dates = await self._service.upcoming_dates(guild_id, ANCHORS)
        return dates, {d.date: d.label for d in dates}

    @staticmethod
    def _label(labels: dict[datetime.date, str], day: datetime.date) -> str:
        return labels.get(day) or texts.schedule_date_label(texts.PROJECTED_EVENT_NAME, day)

    async def overview(self) -> ScheduleOverview | None:
        guild_id = self._guild_id()
        if guild_id is None:
            return None
        db = self._ctx.db
        raw_enabled = await config_db.get_value(db, guild_id, "event_schedule_enabled")
        raw_first = await config_db.get_value(db, guild_id, event_replenish.FIRST_DATE_KEY)
        raw_look = await config_db.get_value(db, guild_id, "event_lookahead_count")
        dates, labels = await self._labels(guild_id)
        rules = await db_rules.list_rules(db, guild_id)
        defaults = {}
        for field in FIELDS:
            value = await db_rules.get_default(db, guild_id, field)
            if value:
                defaults[field] = value
        return ScheduleOverview(
            enabled=event_replenish._schedule_enabled(raw_enabled),
            interval_days=await self._service.interval_days(guild_id),
            first_date=(raw_first or "").strip() or None,
            lookahead=event_replenish._to_int(raw_look, 6),
            defaults=defaults,
            rules=[RuleView(r, self._label(labels, r.anchor)) for r in rules],
            dates=dates[:SHOWN_EVENTS],
            anchors=dates,
            pending=len(await self._service.pending_changes(guild_id)),
        )

    async def has_pending(self) -> bool:
        guild_id = self._guild_id()
        return guild_id is not None and bool(await self._service.pending_changes(guild_id))

    # --- defaults ---

    async def set_defaults(self, genre: str, location: str) -> ActionResult:
        guild_id = self._guild_id()
        if guild_id is None:
            return ActionResult(False, NOT_READY)
        try:
            await self._service.set_default(guild_id, "genre", genre)
            await self._service.set_default(guild_id, "location", location)
        except ValueError as exc:
            return ActionResult(False, str(exc))
        except Exception:
            logger.exception("Saving the schedule defaults failed")
            return ActionResult(False, SAVE_FAILED)
        return ActionResult(True, "The defaults are saved.")

    # --- rules ---

    async def get_rule(self, rule_id: int) -> RuleView | None:
        guild_id = self._guild_id()
        if guild_id is None:
            return None
        rule = await db_rules.get_rule(self._ctx.db, guild_id, rule_id)
        if rule is None:
            return None
        _, labels = await self._labels(guild_id)
        return RuleView(rule, self._label(labels, rule.anchor))

    async def anchor_options(self, current: datetime.date | None = None) -> list[tuple[str, str]]:
        """(label, ISO date) choices; the current anchor stays listed after it has passed."""
        guild_id = self._guild_id()
        if guild_id is None:
            return []
        dates, labels = await self._labels(guild_id)
        options = [(d.label, d.date.isoformat()) for d in dates]
        if current is not None and current not in labels:
            options.insert(0, (self._label(labels, current), current.isoformat()))
        return options

    @staticmethod
    def _parse_rule(
        field: str, value: str, every: str, start: str, rule_id: int | None
    ) -> Rule | str:
        if field not in FIELDS:
            return BAD_FIELD
        every = every.strip()
        if not every.isdigit() or len(every) > 4:
            return texts.RULE_EVERY_RANGE
        try:
            anchor = datetime.date.fromisoformat(start.strip())
        except ValueError:
            return texts.RULE_ANCHOR_NOT_UPCOMING
        return Rule(rule_id, field, value, int(every), anchor)  # type: ignore[arg-type]

    async def _save_rule(
        self, rule_id: int | None, field: str, value: str, every: str, start: str
    ) -> ActionResult:
        guild_id = self._guild_id()
        if guild_id is None:
            return ActionResult(False, NOT_READY)
        rule = self._parse_rule(field, value, every, start, rule_id)
        if isinstance(rule, str):
            return ActionResult(False, rule)
        try:
            if rule_id is None:
                saved = await self._service.add_rule(guild_id, rule)
            else:
                saved = await self._service.edit_rule(guild_id, rule)
        except RuleConflict as exc:
            _, labels = await self._labels(guild_id)
            lines = [
                (r.id, texts.rule_summary(r.field, r.value, r.every, self._label(labels, r.anchor)))
                for r in exc.rules
            ]
            return ActionResult(False, texts.rule_conflict(lines))
        except LookupError:
            return ActionResult(False, texts.RULE_NOT_FOUND)
        except ValueError as exc:
            return ActionResult(False, str(exc))
        except Exception:
            logger.exception("Saving a schedule rule failed")
            return ActionResult(False, SAVE_FAILED)
        _, labels = await self._labels(guild_id)
        line = texts.rule_line(
            saved.id, saved.field, saved.value, saved.every, self._label(labels, saved.anchor)
        )
        make = texts.rule_added if rule_id is None else texts.rule_edited
        return ActionResult(True, make(line))

    async def add_rule(self, field: str, value: str, every: str, start: str) -> ActionResult:
        return await self._save_rule(None, field, value, every, start)

    async def edit_rule(
        self, rule_id: int, field: str, value: str, every: str, start: str
    ) -> ActionResult:
        return await self._save_rule(rule_id, field, value, every, start)

    async def remove_rule(self, rule_id: int) -> ActionResult:
        guild_id = self._guild_id()
        if guild_id is None:
            return ActionResult(False, NOT_READY)
        try:
            removed = await self._service.remove_rule(guild_id, rule_id)
        except Exception:
            logger.exception("Removing a schedule rule failed")
            return ActionResult(False, SAVE_FAILED)
        if not removed:
            return ActionResult(False, texts.RULE_NOT_FOUND)
        return ActionResult(True, texts.rule_removed(rule_id))

    # --- event values ---

    async def get_event(self, uid: str) -> Any | None:
        if self._ctx.db is None:
            return None
        return await db_events.get_event(self._ctx.db, uid)

    async def set_event(self, uid: str, genre: str, location: str, note: str) -> ActionResult:
        guild_id = self._guild_id()
        if guild_id is None:
            return ActionResult(False, NOT_READY)
        try:
            event = await self._service.set_event_values(
                guild_id, uid, genre=genre, location=location, note=note
            )
        except LookupError:
            return ActionResult(False, NO_EVENT)
        except ValueError as exc:
            return ActionResult(False, str(exc))
        except Exception:
            logger.exception("Saving the event values failed")
            return ActionResult(False, SAVE_FAILED)
        return ActionResult(True, f"Saved {texts.event_name(event)}.")

    # --- apply ---

    async def pending(self) -> list[PendingView]:
        guild_id = self._guild_id()
        if guild_id is None:
            return []
        return [
            PendingView(
                p.event.page_id, p.field, texts.change_text(p.event, p.field, p.old, p.new)
            )
            for p in await self._service.pending_changes(guild_id)
        ]

    async def apply(self, choice: str, picks: list[str], shown: list[str]) -> ActionResult:
        """`all`: the changes that were shown; `selected`: the picked ones; `keep`: none."""
        if choice == "keep":
            return ActionResult(True, texts.APPLY_KEPT)
        guild_id = self._guild_id()
        if guild_id is None:
            return ActionResult(False, NOT_READY)
        raw = shown if choice == "all" else picks
        if choice not in ("all", "selected"):
            return ActionResult(False, "Choose what to do with the changes.")
        pairs = {pair for pair in map(parse_pick, raw) if pair is not None}
        if choice == "all":
            pairs &= {pair for pair in map(parse_pick, shown) if pair is not None}
        if not pairs:
            return ActionResult(False, texts.APPLY_NONE_PICKED)
        try:
            count = await self._service.apply(guild_id, pairs)
        except Exception:
            logger.exception("Applying the schedule changes failed")
            return ActionResult(False, SAVE_FAILED)
        return ActionResult(True, texts.apply_done(count))


