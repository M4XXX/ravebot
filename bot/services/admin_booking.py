"""AdminBookingService — admin booking, cancellation, and DJ status management.

Handles /book dj slot, /cancel dj slot and /automatic dj booking slash command logic.
Bypasses the confirmation dialog and DJ-facing race condition guard.

[Source: epics.md#Story 6.3 — FR override commands]
[Source: epics.md#Story 6.4 — DJ active status management]
[Source: architecture.md#Cog Responsibility Boundary — Cog delegates to service]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]

Postgres is the source of truth: an override sets the slot holder there
(force_slot_holder takes the same row lock as a DJ booking, so the two are
serialized) and queues the slot for the Notion mirror. Notion is never awaited.
"""
import logging
from typing import TYPE_CHECKING

from bot.db import events as db_events
from bot.db.bookings import SlotGoneError, force_slot_holder, release_slot_lock
from bot.db.djs import get_dj_by_discord_id, update_active_booking
from bot.services.dm import dm_user
from bot.services.fully_booked import recheck_fully_booked
from bot.services.roles import set_invites_role
from bot.sync.worker import changed
from bot.ui import texts
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    import asyncpg
    from disnake.ext import commands

    from bot.context import BotContext
    from bot.log import AdminLogService
    from bot.notion.models import EventInfo

logger = logging.getLogger(__name__)

EVENTS_FAILED = "Could not read the events. Please try again in a moment."
DB_FAILED = "Database error while writing the slot. Please try again in a moment."


def not_registered(dj_name: str) -> str:
    return (
        f"@{dj_name} is not registered in the system. "
        "They may need to complete onboarding."
    )


class AdminBookingService:
    """Admin booking/cancellation override — bypasses the self-service confirmation flow.

    Both methods return (True, "") on success or (False, error_message) on failure.
    The cog sends the appropriate ephemeral reply based on the return value.

    [Source: architecture.md#Cog Responsibility Boundary]
    """

    def __init__(
        self,
        bot: "commands.Bot",
        db: "asyncpg.Pool",
        log_service: "AdminLogService | None",
        *,
        ctx: "BotContext",
    ) -> None:
        self.bot = bot
        self.db = db
        self.log_service = log_service
        self._ctx = ctx  # for changed(): the Notion mirror queue

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _find_event(self, events: "list[EventInfo]", event_name: str) -> "EventInfo | None":
        """Find first event whose ID case-insensitively matches event_name."""
        for event in events:
            if event.event_id.lower() == event_name.lower():
                return event
        return None

    def _display_name(self, user_id: int) -> str:
        user = self.bot.get_user(user_id) if user_id > 0 else None
        return getattr(user, "display_name", None) or str(user_id)

    async def _dm_dj(self, dj_id: int, text: str, dj_name: str, what: str) -> None:
        """Send a DM to the DJ; a failed DM is non-fatal but reported to the log channel."""
        if await dm_user(self.bot, dj_id, text):
            return
        if self.log_service is None:
            return
        try:
            await self.log_service.post_error(
                error_code=ErrorCode.ERR_DM_FAILED,
                trigger=(
                    f"The {what} notice to {texts.escape_text(dj_name)} could not be "
                    "delivered because their DMs are closed or the bot cannot reach them."
                ),
                action_taken="The change was made anyway.",
                manual_action=texts.dm_failed_todo(dj_name),
            )
        except Exception:
            logger.exception("admin booking: post_error failed for DJ %s", dj_id)

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    async def _find(self, event_name: str, slot_num: int, what: str) -> "EventInfo | str":
        """The event to change, or the error text for the admin."""
        try:
            events = await db_events.list_events(self.db)
        except Exception:
            logger.exception("%s: list_events failed", what)
            return EVENTS_FAILED
        event = self._find_event(events, event_name)
        if event is None:
            return f"Event '{event_name}' not found."
        if not 1 <= slot_num <= event.slot_count:
            return f"'{event.event_id}' has slots 1 to {event.slot_count}."
        return event

    async def book_slot_override(
        self,
        admin_name: str,
        dj_id: int,
        dj_name: str,
        event_name: str,
        slot_num: int,
    ) -> tuple[bool, str]:
        """Force-book a slot for a DJ, bypassing the confirmation dialog.

        Postgres only: the slot's holder becomes this DJ (``force_slot_holder``),
        the Notion mirror follows in the background.

        Args:
            admin_name: Display name of the admin running the command.
            dj_id:      Discord user ID of the DJ to book.
            dj_name:    Display name of the DJ (for log messages).
            event_name: Event ID (case-insensitive match).
            slot_num:   1-based slot number.

        Returns:
            (True, "") on success.
            (False, error_message) on any failure.

        [Source: epics.md#Story 6.3 AC1]
        """
        found = await self._find(event_name, slot_num, "admin book")
        if isinstance(found, str):
            return False, found
        event = found
        event_id = event.page_id
        slot_index = slot_num - 1  # convert to 0-based

        # Only registered DJs can hold a slot (the lineup shows their DJ name)
        try:
            registered = await get_dj_by_discord_id(self.db, dj_id) is not None
        except Exception:
            logger.exception("admin book: get_dj_by_discord_id failed")
            return False, DB_FAILED
        if not registered:
            return False, not_registered(dj_name)

        # The slot's holder becomes exactly this DJ, replacing any displaced holder
        try:
            previous_holder = await force_slot_holder(self.db, event_id, slot_index, dj_id)
        except SlotGoneError:
            return False, texts.SLOT_NO_LONGER_EXISTS_ADMIN
        except Exception:
            logger.exception("admin book: force_slot_holder failed")
            return False, DB_FAILED
        changed(self._ctx, "slot", f"{event_id}:{slot_index}")

        # DM the DJ (non-fatal), and the DJ who lost the slot to this booking
        await self._dm_dj(
            dj_id,
            texts.dm_booked(event, slot_index),
            dj_name,
            "booking",
        )
        if previous_holder is not None and previous_holder != dj_id:
            await self._dm_dj(
                previous_holder,
                texts.dm_cancelled(event, slot_index),
                self._display_name(previous_holder),
                "cancellation",
            )

        # Log override to admin channel
        if self.log_service is not None:
            await self.log_service.log_changed_by_organizer(
                admin_name, "booked", dj_name, event, slot_index
            )

        await recheck_fully_booked(self.db, self.log_service)

        return True, ""

    async def cancel_slot_override(
        self,
        admin_name: str,
        dj_id: int,
        dj_name: str,
        event_name: str,
        slot_num: int,
    ) -> tuple[bool, str]:
        """Force-cancel a DJ's booking, bypassing the self-service confirmation flow.

        Postgres only; the Notion mirror follows in the background.

        Args:
            admin_name: Display name of the admin running the command.
            dj_id:      Discord user ID of the DJ whose booking to cancel.
            dj_name:    Display name of the DJ (for log messages).
            event_name: Event ID (case-insensitive match).
            slot_num:   1-based slot number.

        Returns:
            (True, "") on success.
            (False, error_message) on failure or when no booking found.

        [Source: epics.md#Story 6.3 AC2, AC3]
        """
        found = await self._find(event_name, slot_num, "admin cancel")
        if isinstance(found, str):
            return False, found
        event = found
        event_id = event.page_id
        slot_index = slot_num - 1

        # Free the slot only if this DJ holds it (one statement, no race with a rebooking)
        try:
            released = await release_slot_lock(
                self.db, event_id, slot_index, expected_holder=dj_id
            )
        except Exception:
            logger.exception("admin cancel: release_slot_lock failed")
            return False, DB_FAILED
        if not released:
            return False, f"No booking found for @{dj_name} in that slot."
        changed(self._ctx, "slot", f"{event_id}:{slot_index}")

        # DM the DJ (non-fatal)
        await self._dm_dj(
            dj_id,
            texts.dm_cancelled(event, slot_index),
            dj_name,
            "cancellation",
        )

        # Log override to admin channel
        if self.log_service is not None:
            await self.log_service.log_changed_by_organizer(
                admin_name, "removed", dj_name, event, slot_index
            )

        await recheck_fully_booked(self.db, self.log_service)

        return True, ""

    async def set_dj_active_status(
        self,
        guild_id: int,
        dj_id: int,
        dj_name: str,
        active: bool,
    ) -> tuple[bool, str]:
        """Set a DJ's active_booking status in DB and sync the Active Booking Discord role.

        Returns (True, "") on success.
        Returns (False, error_message) if DJ not found in djs table.
        Role operations are best-effort — failure does not block the DB update.

        [Source: epics.md#Story 6.4 AC1, AC2, AC3]
        """
        # Step 1: update DB — returns False if DJ row doesn't exist
        updated = await update_active_booking(
            self.db, discord_user_id=dj_id, active_booking=active
        )
        if not updated:
            return False, not_registered(dj_name)

        # Step 2: sync Discord role (best-effort — failure does not block success)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("set_dj_active_status: guild %s not in bot cache", guild_id)
            return True, ""
        await set_invites_role(
            self.db, guild, dj_id, add=active, reason="Admin override via /automatic dj booking"
        )

        return True, ""
