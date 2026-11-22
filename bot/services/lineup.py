"""Lineup service: posts a Lineup Update for the upcoming event on every change.

A new post per change (never edited); the bot's previous post for the same event is
deleted once the new one is sent, if nobody has written in the channel since. The last
posted state is kept as a snapshot in config (`lineup_post_snapshot`) so a poll cycle and a
debounced refresh never post the same state twice.
"""
import asyncio
import datetime
import json
import logging
from typing import TYPE_CHECKING, Any

import disnake

from bot.db import events as db_events
from bot.db.config import get_value, set_value
from bot.db.dj_channel_posts import get_last_post, set_last_post
from bot.notion.models import FINISHED_STATUSES, EventInfo
from bot.services import activation
from bot.timezone import local_today
from bot.ui import texts
from bot.ui.embeds import LineupUpdateEmbed

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

LINEUP_DEBOUNCE_SECONDS = 45
SNAPSHOT_KEY = "lineup_post_snapshot"

_SENT = "sent"
_NO_CONFIG = "no_config"
_FAILED = "failed"


def _today() -> datetime.date:
    return local_today()


def future_events(events: list[EventInfo], today: datetime.date) -> list[EventInfo]:
    """Events dated today or later, not Done/Canceled, sorted by date and start."""
    eligible = [
        e for e in events if e.date >= today and e.status not in FINISHED_STATUSES
    ]
    return sorted(eligible, key=lambda e: (e.date, e.start))


def upcoming_event(events: list[EventInfo], today: datetime.date) -> EventInfo | None:
    """The earliest eligible event, or None."""
    eligible = future_events(events, today)
    return eligible[0] if eligible else None


def event_snapshot(event: EventInfo | None) -> str:
    """Content snapshot of an event's schedule and bookings; status is excluded."""
    if event is None:
        return "none"
    return json.dumps(
        {
            "page_id": event.page_id,
            "event_id": event.event_id,
            "date": event.date.isoformat(),
            "start": event.start.strftime("%H:%M"),
            "slot_minutes": event.slot_minutes,
            "slot_count": event.slot_count,
            "genre": event.genre,
            "location": event.location,
            "notes": event.notes,
            "slots": [
                [number, slot.dj_page_id] for number, slot in sorted(event.resolved_slots().items())
            ],
        }
    )


class LineupService:
    """Posts a LineupUpdateEmbed to the DJ channel when the upcoming event changes."""

    def __init__(
        self,
        bot: Any,
        db: Any,
        log_service: "AdminLogService",
        view: disnake.ui.View | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.log_service = log_service
        self.view = view
        self._lock = asyncio.Lock()
        self._pending = False
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def _get_guild_id(self) -> int | None:
        if not self.bot.guilds:
            logger.warning("LineupService: no guilds, skipping")
            return None
        return self.bot.guilds[0].id

    def notify_changed(self) -> None:
        """Schedule one debounced refresh; calls while one is waiting are merged."""
        if self._pending:
            return
        self._pending = True
        task = asyncio.get_running_loop().create_task(self._debounced_refresh())
        self._task = task
        self._tasks.add(task)  # strong reference until done
        task.add_done_callback(self._tasks.discard)

    async def _debounced_refresh(self) -> None:
        try:
            guild_id = self._get_guild_id()
            if guild_id is None or not await activation.is_active(self.db, guild_id):
                return  # idle: nothing is scheduled or posted
            await asyncio.sleep(LINEUP_DEBOUNCE_SECONDS)
            # Cleared before the refresh so a change during it schedules another one
            self._pending = False
            await self.refresh()
        except Exception:
            logger.exception("LineupService: debounced refresh failed")
        finally:
            self._pending = False

    async def refresh(self, events: list[EventInfo] | None = None) -> bool:
        """Post the upcoming event if it differs from the last posted snapshot."""
        async with self._lock:
            guild_id = self._get_guild_id()
            if guild_id is None or not await activation.is_active(self.db, guild_id):
                return False
            loaded = events if events is not None else await db_events.list_events(self.db)
            upcoming = upcoming_event(loaded, _today())
            snapshot = event_snapshot(upcoming)
            stored = await get_value(self.db, guild_id, SNAPSHOT_KEY)
            if snapshot == stored:
                return False
            if upcoming is None:
                await set_value(self.db, guild_id, SNAPSHOT_KEY, snapshot)
                return False
            outcome = await self._send(guild_id, upcoming)
            if outcome == _SENT:
                await set_value(self.db, guild_id, SNAPSHOT_KEY, snapshot)
                return True
            if outcome == _NO_CONFIG:
                # Store anyway so the admin error is not repeated on every poll
                await set_value(self.db, guild_id, SNAPSHOT_KEY, snapshot)
            return False

    async def post_event(self, event: EventInfo) -> bool:
        """Post one event; store its snapshot if it is the current upcoming event."""
        async with self._lock:
            guild_id = self._get_guild_id()
            if guild_id is None or not await activation.is_active(self.db, guild_id):
                return False
            if await self._send(guild_id, event, report_error=False) != _SENT:
                return False
            try:
                current = upcoming_event(await db_events.list_events(self.db), _today())
            except Exception:
                logger.warning("LineupService.post_event: could not load events", exc_info=True)
                return True
            if current is not None and current.page_id == event.page_id:
                await set_value(self.db, guild_id, SNAPSHOT_KEY, event_snapshot(event))
            return True

    async def _send(self, guild_id: int, event: EventInfo, report_error: bool = True) -> str:
        dj_channel_id = await get_value(self.db, guild_id, "dj_channel")
        if dj_channel_id is None:
            logger.warning("LineupService: dj_channel not configured, cannot post lineup")
            if report_error and self.log_service is not None:
                await self.log_service.log_attention(
                    texts.LOG_FAILED_TITLE,
                    "The lineup could not be posted because no DJ channel is set.",
                    texts.DJ_CHANNEL_TODO,
                )
            return _NO_CONFIG
        channel = self.bot.get_channel(int(dj_channel_id))
        if channel is None:
            logger.warning("LineupService: channel %s not found", dj_channel_id)
            return _FAILED
        try:
            record = await get_last_post(self.db, channel.id)
        except Exception:
            logger.warning("LineupService: reading the last lineup post failed", exc_info=True)
            record = None
        # Read before sending: last_message_id moves to the new post once it is sent
        replace_old = (
            record is not None
            and record.event_id == event.page_id
            and getattr(channel, "last_message_id", None) == record.message_id
        )
        kwargs: dict[str, Any] = {"embed": LineupUpdateEmbed(event)}
        if self.view is not None:
            kwargs["view"] = self.view
        try:
            sent = await channel.send(**kwargs)
        except disnake.HTTPException as exc:
            logger.warning("LineupService: sending the lineup post failed: %s", exc)
            return _FAILED
        try:
            await set_last_post(self.db, channel.id, event.page_id, sent.id)
        except Exception:
            logger.warning("LineupService: storing the last lineup post failed", exc_info=True)
            return _SENT
        if replace_old:
            try:
                await channel.get_partial_message(record.message_id).delete()
            except disnake.NotFound:
                logger.debug("LineupService: the old lineup post was already gone")
            except Exception:
                logger.warning("LineupService: deleting the old lineup post failed", exc_info=True)
        return _SENT
