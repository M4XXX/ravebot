"""Intake: hand edits in Notion, checked and applied to Postgres.

The poll (Task 13) hands each ``apply`` and ``new_in_notion`` change from the
planner to ``Intake.apply``. Every change passes the same checks as the command
path (/create event, the admin DJ form, a booking); a change that fails them is
"rejected" and ``last_reasons`` says why, for the caller's warning. After an
apply the mirror records the Notion payload as the agreed state, so the next
cycle sees no difference (or pushes the bot's cleaned values back).

Applied changes are collected and posted as one log entry per cycle
(``flush_log``). ``apply`` never raises.
"""
import datetime
import logging
from typing import Any, Literal

import asyncpg

from bot.db import mirror
from bot.db.bookings import acquire_slot_lock, get_slot_lock_holder, release_slot_lock
from bot.db.djs import (
    dj_name_exists,
    get_dj_row,
    insert_dj,
    next_guest_id,
    update_dj_fields,
)
from bot.db.events import (
    DuplicateEventError,
    EventDraft,
    SlotsBookedError,
    edit_event,
    get_event,
    insert_event,
)
from bot.db.fully_booked import clear_announced
from bot.db.manual_events import mark_manual
from bot.notion.models import EventInfo
from bot.services.dj_validation import (
    check_link,
    check_name,
    check_optional_link,
    check_vrchat,
)
from bot.services.dm import dm_user
from bot.services.event_defaults import validate_event_draft
from bot.services.event_replenish import replenish_guard
from bot.services.fully_booked import recheck_fully_booked
from bot.sync.plan import Change
from bot.sync.worker import changed
from bot.timezone import local_today
from bot.ui import texts
from bot.ui.embeds import _safe_dj_name

__all__ = ["Intake", "IntakeOutcome"]

logger = logging.getLogger(__name__)

IntakeOutcome = Literal["applied", "rejected"]

_EVENT_FIELDS = ("date", "start", "slot_minutes", "slot_count", "status", "genre", "notes")
_EVENT_LABELS = {
    "date": "date", "start": "start", "slot_minutes": "slot length", "slot_count": "slots",
    "status": "status", "genre": "genre", "notes": "notes",
}
_DJ_LABELS = {
    "dj_name": "name", "link": "stream link", "alt_link": "backup link",
    "vrchat_name": "VRChat name",
}


class _Rejected(Exception):
    """A check failed; the message is the reason for the organizer."""


def _ev(name: str) -> str:
    return f"**{texts.escape_text(name)}**"


def _draft(payload: dict[str, Any]) -> EventDraft:
    """The Notion event payload as a draft; _Rejected when a value cannot be read."""
    try:
        hour, minute = str(payload["start"]).split(":")
        return EventDraft(
            event_id=str(payload["event_id"] or ""),
            date=datetime.date.fromisoformat(str(payload["date"])),
            start=datetime.time(int(hour), int(minute)),
            slot_minutes=payload["slot_minutes"],
            slot_count=payload["slot_count"],
            status=payload.get("status") or None,
            genre=payload.get("genre") or None,
            notes=payload.get("notes") or None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _Rejected(f"The event has a value the bot cannot read ({exc}).") from exc


class Intake:
    def __init__(self, ctx: Any, bot: Any) -> None:
        self._ctx = ctx
        self._bot = bot
        self.last_reasons: list[str] = []
        self._applied: list[str] = []

    @property
    def _db(self) -> asyncpg.Pool:
        return self._ctx.db

    async def apply(self, change: Change) -> IntakeOutcome:
        """Check and apply one hand edit; never raises."""
        self.last_reasons = []
        try:
            if change.kind not in ("apply", "new_in_notion") or change.notion is None:
                raise _Rejected(f"The bot does not apply a {change.kind} change by itself.")
            if change.entity == "event":
                await self._event(change)
            elif change.entity == "dj":
                await self._dj(change)
            elif change.entity == "slot":
                await self._slot(change)
            else:
                raise _Rejected(f"Unknown kind of record: {change.entity}.")
        except _Rejected as exc:
            self.last_reasons = [str(exc)]
            return "rejected"
        except Exception as exc:
            logger.exception("Applying a Notion edit failed: %s %s", change.entity, change.key)
            self.last_reasons = [f"Applying the change failed: {str(exc) or repr(exc)}"[:300]]
            return "rejected"
        return "applied"

    async def flush_log(self) -> None:
        """Post the applied changes of this cycle as one entry; never raises."""
        lines, self._applied = self._applied, []
        service = getattr(self._ctx, "log_service", None)
        if not lines or service is None:
            return
        try:
            await service.log_notion_applied(texts.log_notion_applied_text(lines))
        except Exception:
            logger.warning("Posting the applied Notion edits failed", exc_info=True)

    async def _synced(self, entity: str, key: str, page_id: str | None, payload: dict) -> None:
        if page_id is not None:
            await mirror.set_synced(self._db, entity, key, page_id, payload)

    # --- events ---

    async def _event(self, change: Change) -> None:
        notion = change.notion
        assert notion is not None
        draft = _draft(notion)
        if change.key is None:
            await self._new_event(change, draft)
            return
        old = change.pg or {}
        diff = [f for f in _EVENT_FIELDS if notion.get(f) != old.get(f)]
        # An event already past may still get notes or a status; only a new date must be ahead
        today = local_today() if "date" in diff else draft.date - datetime.timedelta(days=1)
        errors = validate_event_draft(draft, today=today)
        if errors:
            raise _Rejected(" ".join(errors))
        uid = change.key
        current = await get_event(self._db, uid)
        if current is None:
            raise _Rejected("The event is no longer in the bot.")
        fields = {f: getattr(draft, f) for f in diff}
        if fields:
            try:
                if not await edit_event(self._db, uid, **fields):
                    raise _Rejected("The event is no longer in the bot.")
            except SlotsBookedError as exc:
                highest = max(exc.booked) + 1
                if draft.slot_count < highest:
                    raise _Rejected(
                        f"Slots cannot go below {highest}: slot {highest} is booked."
                    ) from exc
                raise _Rejected(
                    "Start and slot length cannot change while slots are booked."
                ) from exc
            # Removed slots: the worker trashes their pages. Added slots are left to the
            # sync cycle's slot pass (adopts hand-made pages, pushes missing ones).
            for index in range(draft.slot_count, current.slot_count):
                changed(self._ctx, "slot", f"{uid}:{index}")
        await self._synced("event", uid, change.page_id, notion)
        if fields:
            labels = ", ".join(_EVENT_LABELS[f] for f in diff)
            self._applied.append(f"Event {_ev(draft.event_id)}: {labels} changed.")
            self._notify_lineup()

    async def _new_event(self, change: Change, draft: EventDraft) -> None:
        errors = validate_event_draft(draft, today=local_today())
        if errors:
            raise _Rejected(" ".join(errors))
        # As /create event: the scheduler must not count it before it is marked manual
        async with replenish_guard():
            try:
                event = await insert_event(self._db, draft)
            except DuplicateEventError as exc:
                raise _Rejected(f'An event called "{draft.event_id}" already exists.') from exc
            try:
                await mark_manual(self._db, event.page_id)
            except Exception:
                logger.warning("Could not record %r as manual", draft.event_id, exc_info=True)
        uid = event.page_id
        assert change.notion is not None
        await self._synced("event", uid, change.page_id, change.notion)
        # Writes the Bot ID to the Notion page. Its slots are left to the sync cycle's
        # slot pass, which adopts hand-made slot pages and pushes the missing ones.
        changed(self._ctx, "event", uid)
        self._applied.append(f"New event {_ev(draft.event_id)}.")

    # --- DJs ---

    async def _dj(self, change: Change) -> None:
        notion = change.notion
        assert notion is not None
        if change.key is None:
            await self._new_dj(notion, change.page_id)
            return
        dj_id = int(change.key)
        old = change.pg or {}
        row = await get_dj_row(self._db, dj_id)
        if row is None:
            raise _Rejected("The DJ is no longer in the bot.")
        updates: dict[str, object] = {}
        diff = [f for f in _DJ_LABELS if notion.get(f) != old.get(f)]
        for field in diff:
            raw = notion.get(field)
            if field == "dj_name":
                value, err = check_name(raw or "")
                updates["dj_name"] = value
            elif field == "link":
                value, err = check_link(raw or "", "stream link")
                updates["stream_url_primary"] = value
            elif field == "alt_link":
                value, err = check_optional_link(raw)
                updates["stream_url_backup"] = value
            else:
                value, err = check_vrchat(raw)
                updates["vrchat_name"] = value
            if err:
                raise _Rejected(err)
        name = updates.get("dj_name")
        if isinstance(name, str) and await dj_name_exists(self._db, name, exclude_user_id=dj_id):
            raise _Rejected(f'There is already a DJ called "{name}".')
        if updates:
            try:
                await update_dj_fields(self._db, dj_id, **updates)
            except asyncpg.UniqueViolationError as exc:
                raise _Rejected("Another DJ already has this name.") from exc
        await self._synced("dj", change.key, change.page_id, notion)
        if updates:
            changed(self._ctx, "dj", change.key)  # Notion shows the cleaned values
            shown = name if isinstance(name, str) else row["dj_name"]
            labels = ", ".join(_DJ_LABELS[f] for f in diff)
            self._applied.append(f"DJ {_safe_dj_name(shown)}: {labels} changed.")

    async def _new_dj(self, notion: dict[str, Any], page_id: str | None) -> None:
        name, err = check_name(notion.get("dj_name") or "")
        if err:
            raise _Rejected(err)
        link, err = check_link(notion.get("link") or "", "stream link")
        if err:
            raise _Rejected(err)
        alt, err = check_optional_link(notion.get("alt_link"))
        if err:
            raise _Rejected(err)
        vrchat, err = check_vrchat(notion.get("vrchat_name"))
        if err:
            raise _Rejected(err)
        assert name is not None
        if await dj_name_exists(self._db, name):
            raise _Rejected(f'There is already a DJ called "{name}".')
        raw_id = notion.get("discord_id")
        discord_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else None
        if discord_id is not None and await get_dj_row(self._db, discord_id) is not None:
            raise _Rejected("This Discord user is already a DJ.")
        dj_id = discord_id if discord_id is not None else await next_guest_id(self._db)
        try:
            await insert_dj(
                self._db,
                discord_user_id=dj_id,
                dj_name=name,
                vrchat_name=vrchat,
                logo_url=None,
                stream_url_primary=link,
                stream_url_backup=alt,
                active_booking=discord_id is not None,
            )
        except asyncpg.UniqueViolationError as exc:
            raise _Rejected(f'There is already a DJ called "{name}".') from exc
        key = str(dj_id)
        await self._synced("dj", key, page_id, notion)
        changed(self._ctx, "dj", key)  # Notion gets the final Discord ID
        guest = " (guest)" if discord_id is None else ""
        self._applied.append(f"New DJ {_safe_dj_name(name)}{guest}.")

    # --- slots ---

    async def _slot(self, change: Change) -> None:
        notion = change.notion
        assert notion is not None
        uid = notion.get("event_uid")
        number = notion.get("number")
        event = await get_event(self._db, uid) if isinstance(uid, str) else None
        if event is None:
            raise _Rejected("The event of this slot is not in the bot.")
        if not isinstance(number, int) or not 1 <= number <= event.slot_count:
            raise _Rejected(
                f"{event.event_id} has slots 1 to {event.slot_count}, not {number}."
            )
        uid = event.page_id
        index = number - 1
        key = f"{uid}:{index}"
        if change.key is not None and change.key != key:
            raise _Rejected("The slot page points to another slot than before.")
        desired = notion.get("discord_id")
        expected = (change.pg or {}).get("discord_id")
        current = await get_slot_lock_holder(self._db, uid, index)
        if current == desired:
            await self._synced("slot", key, change.page_id, notion)
            return
        if current != expected:
            holder = await self._dj_name(current) if current is not None else "nobody"
            raise _Rejected(
                f"Slot {number} of {event.event_id} changed in the bot meanwhile"
                f" (now {holder})."
            )
        new_name = None
        if desired is not None:
            row = await get_dj_row(self._db, desired) if isinstance(desired, int) else None
            if row is None:
                raise _Rejected(f"The DJ with Discord ID {desired} is not in the bot's DJ list.")
            new_name = row["dj_name"]
        if current is not None:
            if not await release_slot_lock(self._db, uid, index, expected_holder=current):
                raise _Rejected(f"Slot {number} of {event.event_id} changed in the bot meanwhile.")
            await self._removed(event, index, current)
        if desired is not None:
            assert new_name is not None
            if not await acquire_slot_lock(self._db, uid, index, desired):
                changed(self._ctx, "slot", key)  # Notion shows who holds it now
                self._notify_lineup()
                raise _Rejected(
                    f"Slot {number} of {event.event_id} was booked by someone else meanwhile."
                )
            await self._added(event, index, desired, new_name)
        await self._synced("slot", key, change.page_id, notion)
        self._notify_lineup()
        if desired is not None:
            await recheck_fully_booked(self._db, self._ctx.log_service)
        where = f"{_ev(event.event_id)}, slot {number}"
        if current is None:
            self._applied.append(f"{_safe_dj_name(new_name)} booked in {where}.")
        elif desired is None:
            old = _safe_dj_name(await self._dj_name(current))
            self._applied.append(f"{old} removed from {where}.")
        else:
            old = _safe_dj_name(await self._dj_name(current))
            self._applied.append(f"{_safe_dj_name(new_name)} replaced {old} in {where}.")

    async def _dj_name(self, dj_id: int) -> str:
        row = await get_dj_row(self._db, dj_id)
        return row["dj_name"] if row is not None else str(dj_id)

    def _notify_lineup(self) -> None:
        service = getattr(self._ctx, "lineup_service", None)
        if service is not None:
            service.notify_changed()

    async def _removed(self, event: EventInfo, index: int, dj_id: int) -> None:
        if not await dm_user(self._bot, dj_id, texts.dm_removed(event, index)):
            logger.warning("Notion edit: removal DM to %s failed", dj_id)
        await clear_announced(self._db, event.page_id)
        if self._ctx.log_service is not None:
            name = await self._dj_name(dj_id)
            await self._ctx.log_service.log_changed_in_notion(name, event, index)

    async def _added(self, event: EventInfo, index: int, dj_id: int, name: str) -> None:
        if not await dm_user(self._bot, dj_id, texts.dm_added(event, index)):
            logger.warning("Notion edit: lineup DM to %s failed", dj_id)
        if self._ctx.log_service is not None:
            await self._ctx.log_service.log_booked(name, event, index, "Notion")
