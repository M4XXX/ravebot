"""EventCancelService: cancel one upcoming event and tell everyone concerned.

Behind /cancel event. Order of one run: read the bookings, set the event status
to Canceled in Postgres (a failure stops the run, nothing else changes), empty
the booked slots in Postgres, queue the event and the emptied slots for the
Notion mirror, DM the booked DJs, report DJs the bot cannot reach, post the log
entry, announce in the DJ channel if a DJ was booked, and refresh the lineup post.
Notion is never called here; it follows in the background (changed()).
"""
import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import disnake

from bot.db import events as db_events
from bot.db.bookings import get_slot_locks_for_events, release_slot_lock
from bot.db.config import get_value
from bot.db.djs import get_dj_row
from bot.notion.models import STATUS_CANCELED, EventInfo
from bot.services.dm import dm_user
from bot.services.lineup import future_events
from bot.services.management_lineup import notify_management
from bot.sync.worker import changed
from bot.timezone import local_today
from bot.ui import texts
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    from disnake.ext import commands

    from bot.context import BotContext

logger = logging.getLogger(__name__)

DJ_CHANNEL_KEY = "dj_channel"
_UNKNOWN_DJ = "Unknown DJ"


def _today() -> datetime.date:
    return local_today()


@dataclass(frozen=True)
class BookedDj:
    """One DJ booked on the event, with all their slots.

    dj_id: Discord id; zero or below is a guest; None is a DJ shown in a slot
    without a Discord ID.
    """

    dj_id: int | None
    name: str
    slot_indexes: tuple[int, ...]

    @property
    def reachable(self) -> bool:
        """True if the bot can try a DM."""
        return self.dj_id is not None and self.dj_id > 0


@dataclass(frozen=True)
class CancelPreview:
    event: EventInfo
    djs: tuple[BookedDj, ...]


@dataclass(frozen=True)
class CancelResult:
    ok: bool
    message: str
    notified: tuple[str, ...] = ()
    by_hand: tuple[str, ...] = ()
    failed_slots: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Bookings:
    djs: tuple[BookedDj, ...]
    lock_indexes: tuple[int, ...]  # slots with a lock holder
    row_indexes: tuple[int, ...]  # slots whose event row shows a DJ


class EventCancelService:
    """Cancels events. Reads db and the other services from ctx at call time."""

    def __init__(self, ctx: "BotContext", bot: "commands.Bot") -> None:
        self._ctx = ctx
        self._bot = bot
        # One cancel at a time: a second click re-reads the event and finds it canceled
        self._lock = asyncio.Lock()

    # --- reading ---------------------------------------------------------

    async def _find_event(self, event_name: str) -> EventInfo | None:
        """The cancelable event with that ID: today or later, not Done/Canceled."""
        events = await db_events.list_events(self._ctx.db)  # type: ignore[arg-type]
        wanted = event_name.strip().casefold()
        for event in future_events(events, _today()):
            if event.event_id.casefold() == wanted:
                return event
        return None

    async def _bookings(self, event: EventInfo) -> _Bookings:
        """Booked DJs from the slot locks plus DJs only shown in the event's slot rows."""
        db = self._ctx.db
        locks = await get_slot_locks_for_events(db, [event.page_id])  # type: ignore[arg-type]
        holders = {
            index: lock.discord_user_id
            for (page_id, index), lock in locks.items()
            if page_id == event.page_id and lock.discord_user_id is not None
        }
        rows = {number - 1: row for number, row in event.resolved_slots().items()}
        row_indexes = sorted(
            index for index, row in rows.items() if row.dj_page_id or row.extra_dj_page_ids
        )

        slots: dict[tuple[str, object], list[int]] = {}
        ids: dict[tuple[str, object], int | None] = {}
        names: dict[tuple[str, object], str | None] = {}
        booked = set(holders) | {i for i, row in rows.items() if row.dj_page_id}
        for index in sorted(booked):
            row = rows.get(index)
            holder = holders.get(index)
            row_name = row.dj_name if row is not None else None
            if holder is None and row is not None:
                holder = row.discord_id  # in the slot row only (read between two writes)
            elif row is not None and row.discord_id != holder:
                row_name = None  # the row shows another DJ than the lock holder
            key: tuple[str, object]
            if holder is not None:
                key = ("id", holder)
            else:
                key = ("page", row.dj_page_id if row is not None else index)
            slots.setdefault(key, []).append(index)
            ids[key] = holder
            names[key] = names.get(key) or row_name

        djs = []
        for key, indexes in slots.items():
            dj_id = ids[key]
            name = names[key]
            if not name and dj_id is not None:
                dj_row = await get_dj_row(db, dj_id)  # type: ignore[arg-type]
                name = (dj_row or {}).get("dj_name") or str(dj_id)
            djs.append(BookedDj(dj_id, name or _UNKNOWN_DJ, tuple(indexes)))
        djs.sort(key=lambda dj: dj.slot_indexes[0])
        return _Bookings(tuple(djs), tuple(sorted(holders)), tuple(row_indexes))

    async def preview(self, event_name: str) -> CancelPreview | None:
        """The event and its booked DJs, or None if it cannot be canceled. Changes nothing."""
        if self._ctx.db is None:
            return None
        try:
            event = await self._find_event(event_name)
            if event is None:
                return None
            return CancelPreview(event, (await self._bookings(event)).djs)
        except Exception:
            logger.exception("cancel event: preview of %r failed", event_name)
            return None

    # --- cancelling ------------------------------------------------------

    async def cancel_event(self, admin_name: str, event_name: str) -> CancelResult:
        """Cancel the event and notify. Never raises."""
        if self._ctx.db is None:
            return CancelResult(False, texts.GENERIC_ERROR)
        async with self._lock:
            try:
                return await self._cancel(admin_name, event_name)
            except Exception:
                logger.exception("cancel event: %r failed", event_name)
                return CancelResult(False, texts.GENERIC_ERROR)

    async def _cancel(self, admin_name: str, event_name: str) -> CancelResult:
        try:
            event = await self._find_event(event_name)
        except Exception:
            logger.exception("cancel event: list_events failed")
            return CancelResult(False, texts.GENERIC_ERROR)
        if event is None:
            return CancelResult(False, texts.EVENT_NOT_CANCELABLE)

        # Read the bookings now, not from the confirm preview. A failed read stops the
        # run before anything changed, so no event is canceled with nobody told.
        try:
            bookings = await self._bookings(event)
        except Exception:
            logger.exception("cancel event: reading the bookings of %s failed", event.event_id)
            return CancelResult(False, texts.GENERIC_ERROR)

        try:
            await db_events.set_event_status(self._ctx.db, event.page_id, STATUS_CANCELED)  # type: ignore[arg-type]
        except Exception:
            logger.exception("cancel event: status write for %s failed", event.event_id)
            return CancelResult(False, texts.EVENT_CANCEL_SAVE_FAILED)
        changed(self._ctx, "event", event.page_id)

        # From here on the event is canceled: every later step runs whatever fails.
        failed_slots = await self._remove_bookings(event, bookings)
        notified: list[str] = []
        by_hand: list[str] = []
        for dj in bookings.djs:
            if await self._tell(dj, event):
                notified.append(dj.name)
            else:
                by_hand.append(dj.name)
        await self._log(admin_name, event, bookings.djs, failed_slots)
        if bookings.djs:
            await self._announce(event)
        notify_management(self._ctx)
        lineup = self._ctx.lineup_service
        if lineup is not None:
            try:
                lineup.notify_changed()
            except Exception:
                logger.exception("cancel event: lineup refresh could not be scheduled")
        return CancelResult(
            True,
            texts.event_cancel_done(event, notified, by_hand, failed_slots),
            tuple(notified),
            tuple(by_hand),
            tuple(failed_slots),
        )

    async def _remove_bookings(self, event: EventInfo, bookings: _Bookings) -> list[int]:
        """Empty the booked slots and queue each for the mirror; return the slots kept."""
        failed: list[int] = []
        for index in sorted(set(bookings.lock_indexes) | set(bookings.row_indexes)):
            try:
                await release_slot_lock(self._ctx.db, event.page_id, index)  # type: ignore[arg-type]
            except Exception:
                logger.exception(
                    "cancel event: freeing slot %s of %s failed", index + 1, event.event_id
                )
                failed.append(index)
                continue
            changed(self._ctx, "slot", f"{event.page_id}:{index}")
        return failed

    async def _tell(self, dj: BookedDj, event: EventInfo) -> bool:
        """DM the DJ; True if delivered. An unreachable DJ is reported to the log channel."""
        if dj.reachable:
            assert dj.dj_id is not None
            text = texts.dm_event_canceled(event, dj.slot_indexes)
            if await dm_user(self._bot, dj.dj_id, text):
                return True
            trigger = texts.event_canceled_dm_failed(dj.name, event)
            todo = texts.dm_failed_todo(dj.name)
        else:
            # Guests have no Discord account; dm_user would report success without sending
            trigger = texts.event_canceled_no_discord(dj.name, event)
            todo = texts.tell_canceled_todo(dj.name)
        log = self._ctx.log_service
        if log is None:
            logger.warning("cancel event: %s must be told by hand (no log service)", dj.name)
            return False
        try:
            await log.post_error(
                ErrorCode.ERR_DM_FAILED, trigger, texts.EVENT_CANCELED_ANYWAY, todo
            )
        except Exception:
            logger.exception("cancel event: post_error failed for %s", dj.name)
        return False

    async def _log(
        self,
        admin_name: str,
        event: EventInfo,
        djs: tuple[BookedDj, ...],
        failed_slots: list[int],
    ) -> None:
        log = self._ctx.log_service
        if log is None:
            return
        try:
            lines = [texts.dj_line(dj.name, event, dj.slot_indexes) for dj in djs]
            await log.log_event_canceled(admin_name, event, lines)
            if failed_slots:
                await log.log_attention(
                    texts.LOG_FAILED_TITLE,
                    texts.event_cancel_cleanup_failed(event, failed_slots),
                    texts.EVENT_CANCEL_CLEANUP_TODO,
                )
        except Exception:
            logger.exception("cancel event: log entry for %s failed", event.event_id)

    async def _announce(self, event: EventInfo) -> None:
        """Post the cancel notice in the DJ channel; a failure goes to the log channel."""
        try:
            guilds = self._bot.guilds
            raw = (
                await get_value(self._ctx.db, guilds[0].id, DJ_CHANNEL_KEY)  # type: ignore[arg-type]
                if guilds
                else None
            )
            channel = self._bot.get_channel(int(raw)) if raw else None
            if channel is not None:
                await channel.send(  # type: ignore[union-attr]
                    texts.dj_channel_event_canceled(event),
                    allowed_mentions=disnake.AllowedMentions.none(),
                )
                return
        except Exception:
            logger.exception("cancel event: DJ channel post for %s failed", event.event_id)
        log = self._ctx.log_service
        if log is not None:
            try:
                await log.log_attention(
                    texts.LOG_FAILED_TITLE, texts.EVENT_CANCEL_POST_FAILED, texts.DJ_CHANNEL_TODO
                )
            except Exception:
                logger.exception("cancel event: log_attention failed")
