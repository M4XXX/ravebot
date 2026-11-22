"""DJ management lineup: one lineup message per fully booked event, kept current.

The message (one or more Discord messages, the button view on the first only) is
posted in the configured management channel when an event is fully booked and has
no stored record. Afterwards it is edited in the channel stored with it whenever
its rendered content changes. The bot never deletes a lineup message.

A record with no message ids is suppressed ("Only new" backfill): nothing is
posted for it until the event opens up (the record is then deleted) and fills again.

Message-count changes: a longer lineup posts the extra messages in the stored
channel; a shorter one edits the surplus messages to a one-line placeholder and
keeps their ids, so they are reused if the lineup grows again. A missing main
message (deleted by hand) re-posts the whole lineup in the configured channel and
replaces all stored ids.
"""
import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any, Literal

import disnake

from bot.db import events as db_events
from bot.db import management_lineup as ml_db
from bot.db.config import get_value
from bot.db.djs import get_dj_details
from bot.db.management_lineup import LineupRecord
from bot.notion.models import FINISHED_STATUSES, STATUS_CANCELED, EventInfo
from bot.services import activation
from bot.services.fully_booked import is_fully_booked
from bot.services.lineup import future_events
from bot.timezone import local_today
from bot.ui import texts
from bot.ui.management_lineup import build_channel_messages, content_hash, lineup_state

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

MANAGEMENT_CHANNEL_KEY = "management_channel"
MANAGEMENT_DEBOUNCE_SECONDS = 10

_MISSING = "missing"
_FORBIDDEN = "forbidden"

Messages = list[list[disnake.Embed]]


def _today() -> datetime.date:
    return local_today()


class ManagementLineupService:
    """Posts and edits the lineup messages in the DJ management channel."""

    def __init__(
        self,
        bot: Any,
        db: Any,
        log_service: "AdminLogService | None",
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
        self._reported: set[tuple[int, str]] = set()

    # --- scheduling ---------------------------------------------------------

    def _get_guild_id(self) -> int | None:
        if not self.bot.guilds:
            logger.warning("ManagementLineupService: no guilds, skipping")
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
                return  # idle: nothing is posted or edited
            await asyncio.sleep(MANAGEMENT_DEBOUNCE_SECONDS)
            # Cleared before the refresh so a change during it schedules another one
            self._pending = False
            await self.refresh()
        except Exception:
            logger.exception("ManagementLineupService: debounced refresh failed")
        finally:
            self._pending = False

    # --- refresh ------------------------------------------------------------

    async def refresh(self, events: list[EventInfo] | None = None) -> None:
        """Post or edit the lineup message of every event; never raises."""
        try:
            async with self._lock:
                guild_id = self._get_guild_id()
                if guild_id is None or not await activation.is_active(self.db, guild_id):
                    return
                configured = await self._configured_channel(guild_id)
                loaded = events if events is not None else await db_events.list_events(self.db)
                records = {r.event_id: r for r in await ml_db.list_records(self.db)}
                for event in loaded:
                    try:
                        await self._refresh_event(event, records.get(event.page_id), configured)
                    except Exception:
                        logger.exception(
                            "ManagementLineupService: refresh of event %s failed", event.page_id
                        )
        except Exception:
            logger.exception("ManagementLineupService: refresh failed")

    async def _refresh_event(
        self, event: EventInfo, record: LineupRecord | None, configured: int | None
    ) -> None:
        finished = event.status in FINISHED_STATUSES
        if record is None:
            if configured is None or finished or event.date < _today():
                return
            if not is_fully_booked(event):
                return
            messages, digest = await self._render(event)
            await self._post(event, configured, messages, digest)
            return
        if not record.message_ids:  # suppressed
            if not finished and not is_fully_booked(event):
                await ml_db.delete_record(self.db, event.page_id)
            return
        if finished and event.status != STATUS_CANCELED:
            return  # Done: the message is left as it is
        messages, digest = await self._render(event)
        if digest == record.content_hash:
            return
        await self._edit(event, record, messages, digest, configured)

    async def _render(self, event: EventInfo) -> tuple[Messages, str]:
        """Channel messages and their hash; DJ data is read now."""
        user_ids: list[int] = []
        for _number, slot in sorted(event.resolved_slots().items()):
            uid = slot.discord_id
            if slot.dj_page_id is not None and uid is not None and uid not in user_ids:
                user_ids.append(uid)
        djs = await get_dj_details(self.db, user_ids) if user_ids else {}
        messages = build_channel_messages(event, djs, lineup_state(event))
        return messages, content_hash(messages)

    async def _configured_channel(self, guild_id: int) -> int | None:
        value = await get_value(self.db, guild_id, MANAGEMENT_CHANNEL_KEY)
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            logger.warning(
                "ManagementLineupService: bad %s value %r", MANAGEMENT_CHANNEL_KEY, value
            )
            return None

    def _send_kwargs(self, embeds: list[disnake.Embed], first: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"embeds": embeds}
        if first and self.view is not None:
            kwargs["view"] = self.view
        return kwargs

    async def _report(self, channel_id: int, reason: str) -> None:
        """One admin log entry per (channel, reason) per process."""
        key = (channel_id, reason)
        if key in self._reported:
            return
        self._reported.add(key)
        logger.warning("ManagementLineupService: channel %s problem: %s", channel_id, reason)
        if self.log_service is None:
            return
        try:
            await self.log_service.log_attention(
                texts.LOG_FAILED_TITLE,
                texts.mgmt_channel_problem(channel_id, reason),
                texts.MGMT_CHANNEL_TODO,
            )
        except Exception:
            logger.exception("ManagementLineupService: admin log entry failed")

    async def _post(
        self, event: EventInfo, channel_id: int, messages: Messages, digest: str
    ) -> bool:
        """Send a new lineup; store its ids. A partial send stores what was sent."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            await self._report(channel_id, _MISSING)
            return False
        sent: list[int] = []
        try:
            for index, embeds in enumerate(messages):
                message = await channel.send(**self._send_kwargs(embeds, first=index == 0))
                sent.append(message.id)
        except disnake.Forbidden:
            await self._report(channel_id, _FORBIDDEN)
        except Exception:
            # HTTP errors and network failures (timeouts, disconnects) alike
            logger.exception("ManagementLineupService: posting %s failed", event.page_id)
        else:
            await ml_db.upsert_record(self.db, event.page_id, channel_id, sent, digest)
            return True
        if sent:
            # An empty hash makes the next refresh edit and complete it
            await ml_db.upsert_record(self.db, event.page_id, channel_id, sent, "")
        return False

    async def _edit(
        self,
        event: EventInfo,
        record: LineupRecord,
        messages: Messages,
        digest: str,
        configured: int | None,
    ) -> None:
        channel = self.bot.get_channel(record.channel_id)
        if channel is None:
            await self._report(record.channel_id, _MISSING)
            return
        old = list(record.message_ids)
        stored = list(old)
        try:
            for index, embeds in enumerate(messages):
                if index < len(old):
                    message = await channel.fetch_message(old[index])
                    await message.edit(content=None, **self._send_kwargs(embeds, index == 0))
                else:
                    message = await channel.send(**self._send_kwargs(embeds, first=False))
                    stored.append(message.id)
            for message_id in old[len(messages):]:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(content=texts.MGMT_LINEUP_SURPLUS, embeds=[])
                except disnake.NotFound:
                    stored.remove(message_id)  # placeholder gone: nothing to keep
        except disnake.NotFound:
            await self._repost(event, record, messages, digest, configured)
            return
        except disnake.Forbidden:
            await self._report(record.channel_id, _FORBIDDEN)
            await ml_db.upsert_record(self.db, event.page_id, record.channel_id, stored, "")
            return
        except Exception:
            # HTTP errors and network failures alike: keep every id sent so far
            logger.exception("ManagementLineupService: editing %s failed", event.page_id)
            await ml_db.upsert_record(self.db, event.page_id, record.channel_id, stored, "")
            return
        await ml_db.upsert_record(self.db, event.page_id, record.channel_id, stored, digest)

    async def _repost(
        self,
        event: EventInfo,
        record: LineupRecord,
        messages: Messages,
        digest: str,
        configured: int | None,
    ) -> None:
        """A lineup message is gone: post the whole lineup again, replace all ids."""
        if configured is None:
            logger.info(
                "ManagementLineupService: lineup of %s is gone and no channel is set",
                event.page_id,
            )
            return
        if not await self._post(event, configured, messages, digest):
            # Keep the old ids; an empty hash retries on the next refresh
            current = await ml_db.get_record(self.db, event.page_id)
            if current is not None and current.message_ids == record.message_ids:
                await ml_db.upsert_record(
                    self.db, event.page_id, record.channel_id, record.message_ids, ""
                )

    # --- backfill -----------------------------------------------------------

    async def eligible_backfill_events(
        self, events: list[EventInfo] | None = None
    ) -> list[EventInfo]:
        """Upcoming, not finished, fully booked events without a record, by date."""
        loaded = events if events is not None else await db_events.list_events(self.db)
        recorded = {r.event_id for r in await ml_db.list_records(self.db)}
        return [
            e
            for e in future_events(loaded, _today())
            if is_fully_booked(e) and e.page_id not in recorded
        ]

    async def backfill(
        self, mode: Literal["all", "new"], channel_id: int | None = None
    ) -> int:
        """Post ("all") or suppress ("new") the eligible events; the count handled.

        `channel_id` replaces the configured channel: the setup wizard suppresses
        ("new") before it stores the channel, so no poll can post in between.
        """
        async with self._lock:
            configured = channel_id
            if configured is None:
                guild_id = self._get_guild_id()
                if guild_id is None:
                    return 0
                configured = await self._configured_channel(guild_id)
            if configured is None:
                return 0
            count = 0
            for event in await self.eligible_backfill_events():
                if mode == "new":
                    await ml_db.upsert_record(self.db, event.page_id, configured, (), "")
                    count += 1
                    continue
                try:
                    messages, digest = await self._render(event)
                    if await self._post(event, configured, messages, digest):
                        count += 1
                except Exception:
                    logger.exception(
                        "ManagementLineupService: backfill of %s failed", event.page_id
                    )
            return count


def notify_management(ctx: Any) -> None:
    """Ask the management lineup service for a debounced refresh; never raises."""
    try:
        service = getattr(ctx, "management_lineup_service", None)
        if service is not None:
            service.notify_changed()
    except Exception:
        logger.exception("notify_management failed")
