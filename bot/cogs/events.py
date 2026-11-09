"""Events cog — Notion poll loop, lineup post, day-before reminders.

Story 1.5: DB unavailability guard pattern for scheduled task loops.
Story 3.1: Notion event polling with snapshot-based change detection.
Story 7.1: _auto_create_events() — maintain look-ahead event count in Notion.
Full lineup post (FR10) implemented in Story 3.2.

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: epics.md#Story 1.5 AC1]
[Source: epics.md#Story 3.1 AC1-AC4]
[Source: epics.md#Story 7.1 AC1-AC4]
"""
import datetime
import logging

from disnake.ext import commands, tasks

from bot.cogs.loop_support import first_guild_id, guard_db, prepare_loop, run_cycle
from bot.context import BotContext
from bot.notion.client import NotionWriteError
from bot.notion.models import EventInfo
from bot.services import activation
from bot.services.event_replenish import replenish_events
from bot.services.fully_booked import check_fully_booked
from bot.ui import texts

logger = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    """Notion polling, lineup post updates, and reminder scheduling."""

    bot: commands.Bot  # injected by disnake when the cog is added

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    def cog_load(self) -> None:
        self._poll_notion.start()
        self._check_reminders.start()

    def cog_unload(self) -> None:
        self._poll_notion.cancel()
        self._check_reminders.cancel()

    async def _guard_db(self, operation: str) -> bool:
        """True if the DB answers (see loop_support.guard_db)."""
        return await guard_db(self.ctx, operation)

    def _get_guild_id(self) -> int | None:
        return first_guild_id(self.bot)

    async def _auto_create_events(self, events: list[EventInfo], guild_id: int) -> int:
        """Create Notion events to maintain the configured look-ahead count.

        Thin wrapper around bot.services.event_replenish.replenish_events (shared
        with the setup wizard). Done, canceled and past events do not count.

        Called from _poll_notion() after list_events() succeeds, before the
        lineup refresh. Returns how many events were created; the caller's event
        list does not include them, so a non-zero count makes it reload for the refresh.

        On NotionWriteError: ERR_NOTION_WRITE is already posted by NotionApi.request.
        Logs a warning and continues — remaining events in the loop still attempted.

        [Source: epics.md#Story 7.1 AC1-AC4]
        """
        return await replenish_events(self.ctx.db, self.ctx.notion_client, guild_id, events)

    async def _run_sync_cycle(self, guild_id: int) -> None:
        """Run one NotionSyncService cycle; a failure is logged and never raised.

        The lineup update must not depend on the sync succeeding.
        """
        sync = self.ctx.notion_sync_service
        if sync is None:
            return
        try:
            await sync.run_cycle(guild_id)
        except Exception as exc:
            logger.warning("Notion sync cycle failed — continuing with the poll: %s", exc)
            # A NotionWriteError was already posted by NotionApi.
            if self.ctx.log_service is not None and not isinstance(exc, NotionWriteError):
                await self.ctx.log_service.log_attention(
                    texts.LOG_FAILED_TITLE,
                    "The sync with Notion failed. The lineup update goes on and the "
                    "next poll tries the sync again.",
                    "Check that the Notion integration still has access to the pages.",
                )

    @tasks.loop(minutes=5)
    async def _poll_notion(self) -> None:
        await run_cycle("notion poll", self._poll_notion_cycle)

    async def _poll_notion_cycle(self) -> None:
        """Notion event polling — syncs hand edits, fetches events, updates the lineup.

        Order per cycle: (1) NotionSyncService.run_cycle (skipped if the service is
        None; a failure is posted and the cycle continues), (2) list_events() for the
        state after the sync, (3) auto-create events, (4) lineup refresh.
        The first iteration runs after the bot is ready and is the startup reconcile.

        On DB unavailability: skips cycle (guard handles logging).
        On Notion read failure: logs warning, posts ERR_NOTION_WRITE, skips cycle
        (no snapshot update).
        The lineup refresh compares the upcoming event with the last posted snapshot.

        [Source: epics.md#Story 3.1 AC1-AC4]
        [Source: architecture.md#Scheduled Work: disnake tasks.loop]
        """
        if not await self._guard_db("notion_poll"):
            return

        guild_id = self._get_guild_id()
        if guild_id is None:
            return
        if not await activation.is_active(self.ctx.db, guild_id):
            logger.debug("_poll_notion: bot is idle, skipping cycle")
            return

        # Reconcile hand edits in Notion with Postgres before reading the events
        await self._run_sync_cycle(guild_id)

        # Fetch events from Notion (AC1, AC4)
        listed_at = datetime.datetime.now(datetime.UTC)
        try:
            events = await self.ctx.notion_client.list_events()
        except Exception as exc:
            logger.warning("Notion poll failed — skipping cycle: %s", exc)
            # A NotionWriteError was already posted by NotionApi.
            if self.ctx.log_service is not None and not isinstance(exc, NotionWriteError):
                await self.ctx.log_service.log_attention(
                    texts.LOG_FAILED_TITLE,
                    "The bot could not read the events from Notion. "
                    "It tries again on the next poll.",
                    "Check that the Notion integration still has access to the pages.",
                )
            return

        # Story 7.1: auto-create events to maintain look-ahead count (AC1-AC4)
        created = await self._auto_create_events(events, guild_id)

        # Announce events that became fully booked (and clear the flag of reopened ones)
        await check_fully_booked(
            self.ctx.db, self.ctx.log_service, events, listed_at=listed_at
        )

        # Post a Lineup Update if the upcoming event changed since the last post
        if self.ctx.lineup_service is None:
            logger.warning("lineup_service not available — skipping lineup update")
            return
        try:
            if created:
                # The list loaded above lacks the new events: let refresh() reload it
                await self.ctx.lineup_service.refresh()
            else:
                await self.ctx.lineup_service.refresh(events)
        except Exception as exc:
            logger.warning("Lineup refresh failed — retrying next cycle: %s", exc)

    @_poll_notion.before_loop
    async def _before_poll_notion(self) -> None:
        await prepare_loop(self.bot, self.ctx.db)

    @tasks.loop(hours=1)
    async def _check_reminders(self) -> None:
        await run_cycle("reminder check", self._check_reminders_cycle)

    async def _check_reminders_cycle(self) -> None:
        """Reminder scheduler loop — delegates to RemindersService.send_reminders.

        Guard ensures DB is available before any DB-dependent work.
        """
        if not await self._guard_db("reminder_check"):
            return
        guild_id = self._get_guild_id()
        if guild_id is None:
            return
        if not await activation.is_active(self.ctx.db, guild_id):
            logger.debug("_check_reminders: bot is idle, skipping")
            return
        if self.ctx.reminders_service is None:
            logger.warning("_check_reminders: reminders_service not available — skipping")
            return
        try:
            await self.ctx.reminders_service.send_reminders(guild_id, self.ctx)
        except Exception as exc:
            logger.warning("_check_reminders: send_reminders raised: %s", exc)

    @_check_reminders.before_loop
    async def _before_check_reminders(self) -> None:
        await prepare_loop(self.bot, self.ctx.db)
