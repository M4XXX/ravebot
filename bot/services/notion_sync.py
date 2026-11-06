"""NotionSyncService: applies plan_sync() actions between Notion and Postgres.

Notion is the source of truth for slot assignments; Postgres slot_locks only
guard against two DJs booking one slot at the same moment. Each cycle reads
the events from Notion and the slot locks from Postgres, plans with plan_sync()
and applies the actions:

- adopt:        a DJ entered by hand in Notion becomes a booking (lock, DM, log).
- free:         a DJ cleared by hand in Notion loses the booking (unlock, DM, log).
                A lock younger than 120 s (FREE_GRACE) is a booking in flight and is
                never freed, even if its Notion row is still empty.
- revert:       Notion shows a different DJ than the Postgres holder, or several
                DJs: the row is rewritten (holder restored, or extras removed) and
                logged. To replace a booked DJ by hand, clear the cell first, wait
                for one sync cycle (the slot is freed), then enter the new DJ.
- create_slots/trim_slots: Slots rows follow the event's slot count.
- skip:         a row above the slot count that holds a DJ; logged once per
                process, never posted to the admin channel.

Every action runs in its own try/except: one failure never stops the others,
and run_cycle only raises when the initial Notion read or lock query fails.
"""
import datetime
import logging
from typing import TYPE_CHECKING

from bot.db.bookings import (
    acquire_slot_lock,
    get_slot_locks_for_events,
    init_event_slots,
    release_slot_lock,
)
from bot.notion.client import NotionWriteError
from bot.services.dj_pages import ensure_dj_page
from bot.services.dm import dm_user
from bot.services.notion_sync_plan import SyncAction, plan_sync
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    import asyncpg
    from disnake.ext import commands

    from bot.log import AdminLogService
    from bot.notion.client import NotionClient

logger = logging.getLogger(__name__)

_PREFIX = "[NOTION SYNC]"


class NotionSyncService:
    """Reconciles hand edits in Notion with the Postgres slot locks."""

    def __init__(
        self,
        bot: "commands.Bot",
        db: "asyncpg.Pool",
        notion_client: "NotionClient",
        log_service: "AdminLogService | None",
    ) -> None:
        self.bot = bot
        self.db = db
        self.notion_client = notion_client
        self.log_service = log_service
        self._skips_logged: set[tuple[str, str]] = set()

    async def run_cycle(self, guild_id: int) -> list[SyncAction]:
        """Plan and apply one sync cycle; return the planned actions."""
        events = await self.notion_client.list_events()
        locks = await get_slot_locks_for_events(self.db, [e.page_id for e in events])
        actions = plan_sync(events, locks, datetime.datetime.now(datetime.UTC))
        for action in actions:
            try:
                await self._apply(action)
            except Exception:
                logger.exception("Notion sync (guild %s): %s failed", guild_id, action.detail)
        return actions

    # ------------------------------------------------------------------ helpers

    async def _log_line(self, text: str) -> None:
        logger.info("%s", text)
        if self.log_service is not None:
            await self.log_service.post_admin_override(f"{_PREFIX} {text}")

    async def _post_error(self, action: SyncAction, problem: str, manual: str) -> None:
        if self.log_service is None:
            return
        await self.log_service.post_error(
            error_code=ErrorCode.ERR_NOTION_WRITE,
            trigger=f"Notion sync: {action.detail}",
            action_taken=problem,
            manual_action=manual,
        )

    async def _notion_failed(self, action: SyncAction, exc: Exception) -> None:
        """Log a failed Notion write; NotionApi already posts NotionWriteError itself."""
        logger.exception("Notion sync: Notion write failed for %s", action.detail)
        if isinstance(exc, NotionWriteError):
            return
        await self._post_error(
            action,
            f"Notion write failed ({type(exc).__name__}: {exc}); Notion left as is.",
            "Check the event's Slots rows in Notion; the next sync retries.",
        )

    def _when(self, action: SyncAction) -> str:
        assert action.slot_index is not None
        return f"<t:{int(action.event.slot_start(action.slot_index).timestamp())}:f>"

    # ------------------------------------------------------------------ actions

    async def _apply(self, action: SyncAction) -> None:
        if action.kind == "adopt":
            await self._adopt(action)
        elif action.kind == "free":
            await self._free(action)
        elif action.kind == "revert":
            await self._revert(action)
        elif action.kind == "create_slots":
            await self._create_slots(action)
        elif action.kind == "trim_slots":
            await self._trim_slots(action)
        elif action.kind == "skip":
            self._skip(action)

    async def _adopt(self, action: SyncAction) -> None:
        event, index, dj = action.event, action.slot_index, action.discord_id
        assert index is not None and dj is not None
        if not await acquire_slot_lock(self.db, event.page_id, index, dj):
            logger.info("Notion sync: %s; slot taken meanwhile, nothing adopted", action.detail)
            return
        await dm_user(
            self.bot,
            dj,
            f"An organizer has added you to the lineup for {event.event_id} "
            f"at {self._when(action)}.",
        )
        await self._log_line(f"{dj} was added to {event.event_id} slot {index + 1} in Notion")

    async def _free(self, action: SyncAction) -> None:
        event, index, dj = action.event, action.slot_index, action.discord_id
        assert index is not None and dj is not None
        await release_slot_lock(self.db, event.page_id, index)
        await dm_user(
            self.bot,
            dj,
            f"Your booking for {event.event_id} at {self._when(action)} was removed in Notion.",
        )
        await self._log_line(
            f"{dj} was removed from {event.event_id} slot {index + 1} in Notion"
        )

    async def _revert(self, action: SyncAction) -> None:
        event, index, holder = action.event, action.slot_index, action.discord_id
        assert index is not None
        if holder is None:
            try:
                await self.notion_client.keep_first_dj(event, index)
            except Exception as exc:
                await self._notion_failed(action, exc)
                return
            await self._log_line(
                f"extra DJs were removed from slot {index + 1} of {event.event_id}; "
                "the first DJ was kept"
            )
            return
        name = str(self.bot.get_user(holder) or holder)
        try:
            page_id = await ensure_dj_page(self.db, self.notion_client, holder, name)
        except Exception:
            logger.exception("Notion sync: ensure_dj_page failed for %s", holder)
            page_id = None
        if page_id is None:
            await self._post_error(
                action,
                f"Could not find or create the DJ page of holder {holder}; Notion left as is.",
                f"Fix slot {index + 1} of {event.event_id} in Notion by hand.",
            )
            return
        try:
            await self.notion_client.force_assign_slot(event, index, page_id)
        except Exception as exc:
            await self._notion_failed(action, exc)
            return
        await self._log_line(
            f"a hand edit on slot {index + 1} of {event.event_id} was reverted to {holder}"
        )

    async def _create_slots(self, action: SyncAction) -> None:
        event = action.event
        try:
            await self.notion_client.create_slot_rows(event, list(action.numbers))
        except Exception as exc:
            await self._notion_failed(action, exc)
            return
        await init_event_slots(self.db, event.page_id, event.slot_count)
        await self._log_line(action.detail)

    async def _trim_slots(self, action: SyncAction) -> None:
        numbers = set(action.numbers)
        # Only empty rows: a duplicate row with the same number may hold a DJ.
        rows = [
            row
            for row in action.event.slots
            if row.number in numbers and row.dj_page_id is None and not row.extra_dj_page_ids
        ]
        try:
            await self.notion_client.trash_slot_rows(rows)
        except Exception as exc:
            await self._notion_failed(action, exc)
            return
        await self._log_line(action.detail)

    def _skip(self, action: SyncAction) -> None:
        key = (action.event.page_id, action.detail)
        if key in self._skips_logged:
            return
        self._skips_logged.add(key)
        logger.warning("Notion sync: %s", action.detail)
