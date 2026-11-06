"""AdminBookingService — admin booking, cancellation, and DJ status management.

Handles /admin book, /admin cancel, and /admin dj set-active slash command logic.
Bypasses the confirmation dialog and DJ-facing race condition guard.

[Source: epics.md#Story 6.3 — FR override commands]
[Source: epics.md#Story 6.4 — DJ active status management]
[Source: architecture.md#Cog Responsibility Boundary — Cog delegates to service]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]

Admin overrides do not take the FOR UPDATE slot lock (admin_force_book_slot is a
plain UPSERT). A DJ booking racing an admin override is resolved by the Notion
write order (force_assign_slot replaces any holder) and the sync task (Notion wins).
"""
import logging
from typing import TYPE_CHECKING

import disnake

from bot.db.bookings import admin_force_book_slot, get_slot_lock_holder, release_slot_lock
from bot.db.djs import update_active_booking
from bot.notion.client import NotionWriteError, SlotRowMissingError
from bot.services.dj_pages import ensure_dj_page
from bot.services.dm import dm_user
from bot.ui import texts

if TYPE_CHECKING:
    import asyncpg
    from disnake.ext import commands

    from bot.log import AdminLogService
    from bot.notion.client import NotionClient
    from bot.notion.models import EventInfo

logger = logging.getLogger(__name__)


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
        notion_client: "NotionClient | None",
        log_service: "AdminLogService | None",
    ) -> None:
        self.bot = bot
        self.db = db
        self.notion_client = notion_client
        self.log_service = log_service

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _find_event(self, events: "list[EventInfo]", event_name: str) -> "EventInfo | None":
        """Find first event whose ID case-insensitively matches event_name."""
        for event in events:
            if event.event_id.lower() == event_name.lower():
                return event
        return None

    async def _dj_page_id(self, dj_id: int, dj_name: str, what: str) -> str | None:
        """ensure_dj_page, never raising; None if the DJ has no Notion page."""
        assert self.notion_client is not None
        try:
            return await ensure_dj_page(self.db, self.notion_client, dj_id, dj_name)
        except Exception:
            logger.exception("%s: ensure_dj_page failed for %s", what, dj_id)
            return None

    async def _dm_dj(self, dj_id: int, text: str) -> None:
        """Send a DM to the DJ. Swallows Forbidden/NotFound — DM failure is non-fatal."""
        await dm_user(self.bot, dj_id, text)

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    async def book_slot_override(
        self,
        admin_name: str,
        dj_id: int,
        dj_name: str,
        event_name: str,
        slot_num: int,
    ) -> tuple[bool, str]:
        """Force-book a slot for a DJ, bypassing the confirmation dialog.

        Args:
            admin_name: Display name of the admin running the command.
            dj_id:      Discord user ID of the DJ to book.
            dj_name:    Display name of the DJ (for log messages).
            event_name: Notion event ID (case-insensitive match).
            slot_num:   1-based slot number.

        Returns:
            (True, "") on success.
            (False, error_message) on any failure.

        [Source: epics.md#Story 6.3 AC1]
        """
        if self.notion_client is None:
            return False, "The bot is still connecting to Notion. Please try again in a moment."

        # Step 1: fetch future events
        try:
            events = await self.notion_client.list_events()
        except Exception:
            logger.exception("admin book: list_events failed")
            return False, "Could not fetch events from Notion. Please try again in a moment."

        # Step 2: find matching event
        event = self._find_event(events, event_name)
        if event is None:
            return False, f"Event '{event_name}' not found in Notion."

        event_id = event.page_id
        slot_index = slot_num - 1  # convert to 0-based
        slot_unix = int(event.slot_start(slot_index).timestamp())

        # Step 3a: remember the current Postgres holder, to restore it if Notion fails
        try:
            previous_holder = await get_slot_lock_holder(self.db, event_id, slot_index)
        except Exception:
            logger.exception("admin book: get_slot_lock_holder failed")
            previous_holder = None

        # Step 3b: force-book in PostgreSQL slot_locks (UPSERT, bypasses race guard)
        try:
            await admin_force_book_slot(self.db, event_id, slot_index, dj_id)
        except Exception:
            logger.exception("admin book: admin_force_book_slot failed")
            return False, "Database error while writing the slot lock. Please try again in a moment."

        # Step 4: write to Notion — the slot row's DJ becomes exactly this DJ,
        # replacing any displaced holder (retry x3 built into the API layer)
        notion_ok = False
        dj_page_id = await self._dj_page_id(dj_id, dj_name, "admin book")
        if dj_page_id is not None:
            try:
                await self.notion_client.force_assign_slot(event, slot_index, dj_page_id)
                notion_ok = True
            except (NotionWriteError, SlotRowMissingError):
                logger.exception("admin book: force_assign_slot failed")
            except Exception:
                logger.exception("admin book: unexpected error in force_assign_slot")
        if not notion_ok:
            # Undo the DB slot claim: give the slot back to the DJ who held it
            # (Notion still shows them), otherwise free it so it isn't orphaned
            if previous_holder is not None and previous_holder != dj_id:
                try:
                    await admin_force_book_slot(
                        self.db, event_id, slot_index, previous_holder
                    )
                except Exception:
                    logger.exception(
                        "admin book: could not restore previous holder %s after Notion error",
                        previous_holder,
                    )
            else:
                try:
                    await release_slot_lock(
                        self.db, event_id, slot_index, expected_holder=dj_id
                    )
                except Exception:
                    logger.exception(
                        "admin book: release_slot_lock failed after Notion error"
                    )
            return False, "Notion write failed after 3 attempts. Please try again in a moment."

        # Step 5: DM the DJ (non-fatal)
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "your slot"
        await self._dm_dj(
            dj_id,
            texts.dm_booked(event, slot_index),
        )

        # Step 6: log override to admin channel
        if self.log_service is not None:
            msg = (
                f"[ADMIN OVERRIDE] @{admin_name} booked @{dj_name}"
                f" into {event_name} at {slot_display}"
            )
            await self.log_service.post_admin_override(msg)

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

        Args:
            admin_name: Display name of the admin running the command.
            dj_id:      Discord user ID of the DJ whose booking to cancel.
            dj_name:    Display name of the DJ (for log messages).
            event_name: Notion event ID (case-insensitive match).
            slot_num:   1-based slot number.

        Returns:
            (True, "") on success.
            (False, error_message) on failure or when no booking found.

        [Source: epics.md#Story 6.3 AC2, AC3]
        """
        if self.notion_client is None:
            return False, "The bot is still connecting to Notion. Please try again in a moment."

        # Step 1: fetch future events
        try:
            events = await self.notion_client.list_events()
        except Exception:
            logger.exception("admin cancel: list_events failed")
            return False, "Could not fetch events from Notion. Please try again in a moment."

        # Step 2: find matching event
        event = self._find_event(events, event_name)
        if event is None:
            return False, f"Event '{event_name}' not found in Notion."

        event_id = event.page_id
        slot_index = slot_num - 1

        # Step 3: verify this DJ holds the slot (AC3)
        try:
            holder = await get_slot_lock_holder(self.db, event_id, slot_index)
        except Exception:
            logger.exception("admin cancel: get_slot_lock_holder failed")
            return False, "Database error while checking the slot. Please try again in a moment."

        if holder != dj_id:
            return False, f"No booking found for @{dj_name} in that slot."

        slot_unix = int(event.slot_start(slot_index).timestamp())

        # Step 4: remove this DJ from the Notion slot row (retry x3 built in).
        # Do NOT return on failure — the slot lock must always be freed (AC2).
        notion_error = False
        dj_page_id = await self._dj_page_id(dj_id, dj_name, "admin cancel")
        if dj_page_id is None:
            notion_error = True
        else:
            try:
                if not await self.notion_client.clear_slot(event, slot_index, dj_page_id):
                    logger.warning(
                        "admin cancel: slot %s of %r: Notion slot held by another DJ;"
                        " sync will reconcile",
                        slot_num,
                        event.event_id,
                    )
            except (NotionWriteError, SlotRowMissingError) as exc:
                logger.error("admin cancel: Notion clear_slot failed: %s", exc)
                notion_error = True
            except Exception:
                logger.exception("admin cancel: unexpected error in clear_slot")
                notion_error = True

        # Step 5: always release slot lock even if Notion write failed
        try:
            await release_slot_lock(self.db, event_id, slot_index, expected_holder=dj_id)
        except Exception:
            logger.exception("admin cancel: release_slot_lock failed")

        if notion_error:
            return False, "Notion write failed. The slot lock is released, but Notion may be out of sync. Check the slot in Notion."

        # Step 6: DM the DJ (non-fatal)
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "your slot"
        await self._dm_dj(
            dj_id,
            texts.dm_cancelled(event, slot_index),
        )

        # Step 7: log override to admin channel
        if self.log_service is not None:
            msg = (
                f"[ADMIN OVERRIDE] @{admin_name} cancelled @{dj_name}"
                f" from {event_name} at {slot_display}"
            )
            await self.log_service.post_admin_override(msg)

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
            return (
                False,
                f"@{dj_name} is not registered in the system. "
                "They may need to complete onboarding.",
            )

        # Step 2: sync Discord role (best-effort — failure does not block success)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("set_dj_active_status: guild %s not in bot cache", guild_id)
            return True, ""
        member = guild.get_member(dj_id)
        if member is None:
            logger.warning(
                "set_dj_active_status: member %s not in guild cache", dj_id
            )
            return True, ""
        role = disnake.utils.get(guild.roles, name="Active Booking")
        if role is None:
            logger.warning(
                "set_dj_active_status: 'Active Booking' role not found in guild %s",
                guild_id,
            )
            return True, ""
        try:
            if active:
                await member.add_roles(
                    role, reason="Admin override via /admin dj set-active"
                )
            else:
                await member.remove_roles(
                    role, reason="Admin override via /admin dj set-active"
                )
        except (disnake.Forbidden, disnake.HTTPException) as exc:
            logger.warning(
                "set_dj_active_status: role op failed for user %s: %s", dj_id, exc
            )

        return True, ""
