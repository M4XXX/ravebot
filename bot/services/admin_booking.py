"""AdminBookingService — admin booking, cancellation, and DJ status management.

Handles /admin book, /admin cancel, and /admin dj set-active slash command logic.
Bypasses the confirmation dialog and DJ-facing race condition guard.

[Source: epics.md#Story 6.3 — FR override commands]
[Source: epics.md#Story 6.4 — DJ active status management]
[Source: architecture.md#Cog Responsibility Boundary — Cog delegates to service]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
"""
import datetime
import logging
from typing import TYPE_CHECKING

import disnake

from bot import schedule
from bot.db.bookings import (
    admin_force_book_slot,
    get_slot_lock_holder,
    hand_back_slot,
    holds_slot_in_event,
    release_slot_lock,
)
from bot.db.djs import get_dj_by_discord_id, get_notion_dj_page_id, update_active_booking
from bot.notion import props
from bot.notion.client import NotionSlotTakenError, NotionWriteError

if TYPE_CHECKING:
    import asyncpg
    from disnake.ext import commands

    from bot.log import AdminLogService
    from bot.notion.client import NotionClient

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

    def _get_event_name(self, event: dict) -> str:
        """Extract plain text event name from a Notion page object."""
        try:
            return props.title_text(event)
        except (KeyError, IndexError, TypeError):
            return ""

    def _get_slot_unix(self, event: dict, slot_index: int) -> int:
        """Unix timestamp of a slot start (bot.schedule); 0 if the event date is unparseable."""
        return schedule.slot_unix(event, slot_index)

    def _find_event(self, events: list[dict], query: str) -> tuple[dict | None, str]:
        """The event an admin means: by date (YYYY-MM-DD or DD.MM.YYYY) or by name.

        Returns (event, "") or (None, reason). Organizers often give every night the same
        name ("Casual Event"); a name or date that fits several upcoming events is refused
        instead of silently taking the first one.
        """
        query = query.strip()
        day = _parse_day(query)
        if day is not None:
            matches = [e for e in events if _event_day(e) == day]
        else:
            matches = [e for e in events if self._get_event_name(e).lower() == query.lower()]
        if not matches:
            return None, f"Event '{query}' not found in Notion."
        if len(matches) > 1:
            if day is not None:
                names = ", ".join(self._get_event_name(e) or "?" for e in matches)
                return None, (
                    f"{len(matches)} events on {day.isoformat()} ({names}) — "
                    "remove the duplicate in Notion first."
                )
            dates = ", ".join(d.isoformat() for e in matches if (d := _event_day(e)))
            return None, (
                f"{len(matches)} upcoming events are called '{query}' — "
                f"give the date instead ({dates})."
            )
        return matches[0], ""

    async def _cell_name(self, dj_id: int | None) -> str | None:
        """The DJ's name as written into the plan (tds layout), or None."""
        if dj_id is None or self.notion_client is None or self.notion_client.per_slot is not True:
            return None
        dj = await get_dj_by_discord_id(self.db, dj_id)
        return dj["dj_name"] if dj is not None else None

    async def _dm_dj(self, dj_id: int, text: str) -> None:
        """Send a DM to the DJ. Swallows Forbidden/NotFound — DM failure is non-fatal."""
        try:
            user = self.bot.get_user(dj_id)
            if user is None:
                user = await self.bot.fetch_user(dj_id)
            await user.send(text)
        except disnake.NotFound:
            logger.warning("admin override DM: user %s not found", dj_id)
        except disnake.Forbidden:
            logger.warning("admin override DM: DMs blocked for user %s", dj_id)
        except Exception:
            logger.exception("admin override DM: unexpected error for user %s", dj_id)

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
            event_name: Notion event name (case-insensitive match).
            slot_num:   1-based slot number.

        Returns:
            (True, "") on success, or (True, note) when it worked but needs a look in Notion.
            (False, error_message) on any failure.

        [Source: epics.md#Story 6.3 AC1]
        """
        if self.notion_client is None:
            return False, "Bot is still connecting to Notion — please try again."

        # Step 1: fetch future events
        try:
            events = await self.notion_client.poll_events()
        except Exception:
            logger.exception("admin book: poll_events failed")
            return False, "Could not fetch events from Notion — please try again."

        # Step 2: find matching event
        event, problem = self._find_event(events, event_name)
        if event is None:
            return False, problem
        event_name = self._get_event_name(event) or event_name

        event_id = event["id"]
        slot_index = slot_num - 1  # convert to 0-based

        # Step 3: compute slot unix
        slot_unix = self._get_slot_unix(event, slot_index)

        # Step 3b: who holds the slot now? A displaced DJ is unlinked in Notion after the
        # new booking is written, and gets the slot back if that write fails.
        try:
            previous_holder = await get_slot_lock_holder(self.db, event_id, slot_index)
        except Exception:
            logger.exception("admin book: get_slot_lock_holder failed")
            return False, "Database error checking slot — please try again."
        displaced = previous_holder if previous_holder not in (None, dj_id) else None

        # Step 4: get DJ's Notion page ID (None is acceptable — best-effort)
        try:
            dj_notion_page_id = await get_notion_dj_page_id(self.db, dj_id)
        except Exception:
            logger.warning("admin book: get_notion_dj_page_id failed for %s", dj_id)
            dj_notion_page_id = None
        # tds layout: the plan gets the DJ's name; a displaced DJ's name is overwritten
        try:
            cell_name = await self._cell_name(dj_id)
            replaces = await self._cell_name(displaced)
        except Exception:
            logger.exception("admin book: reading DJ names failed")
            return False, "Database error reading the DJ — please try again."

        # Step 5: force-book in PostgreSQL slot_locks (UPSERT, bypasses race guard)
        try:
            await admin_force_book_slot(self.db, event_id, slot_index, dj_id)
        except Exception:
            logger.exception("admin book: admin_force_book_slot failed")
            return False, "Database error writing slot lock — please try again."

        # Step 6: write to Notion (retry x3 built into write_booking)
        try:
            await self.notion_client.write_booking(
                event_id,
                slot_index,
                dj_notion_page_id,
                [],
                event_name,
                slot_unix,
                dj_name=cell_name,
                replaces=replaces,
            )
        except NotionWriteError as exc:
            # Undo the DB claim: the slot goes back to whoever held it before, but only
            # while it is still ours (the retries take up to ~30 s; it may have changed hands)
            undone = True
            try:
                if previous_holder is None:
                    await release_slot_lock(self.db, event_id, slot_index, dj_id)
                elif displaced is not None:
                    undone = await hand_back_slot(
                        self.db, event_id, slot_index, dj_id, displaced
                    )
            except Exception:
                logger.exception("admin book: could not undo the slot claim after Notion error")
                undone = False
            if not undone:
                return False, (
                    "Notion write failed, and the slot could not be given back to the DJ who "
                    "had it — check it with /ravebot-status."
                )
            if isinstance(exc, NotionSlotTakenError):
                return False, (
                    f"Slot {slot_num} already says '{exc.current}' in the plan — the bot never "
                    "overwrites what was typed in by hand. Clear it in Notion first."
                )
            return False, "Notion write failed after 3 attempts — please try again."

        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "your slot"

        # Step 6b: the displaced DJ leaves the Notion lineup unless they keep another slot
        # (tds layout: their name in this slot's column was already overwritten above)
        note = ""
        if displaced is not None:
            try:
                if self.notion_client.per_slot is not True and not await holds_slot_in_event(
                    self.db, event_id, displaced
                ):
                    await self.notion_client.delete_booking(
                        event_id,
                        await get_notion_dj_page_id(self.db, displaced),
                        [],
                        event_name,
                        slot_unix,
                    )
            except Exception:
                logger.exception(
                    "admin book: could not unlink displaced DJ %s in Notion", displaced
                )
                note = (
                    "The DJ who had this slot may still be linked to the event in Notion — "
                    "please check it there."
                )
            await self._dm_dj(
                displaced,
                f"An organizer has given your slot for {event_name} at {slot_display} "
                "to another DJ.",
            )

        # Step 7: DM the DJ (non-fatal)
        await self._dm_dj(
            dj_id,
            f"An organizer has booked you for {event_name} at {slot_display}.",
        )

        # Step 8: log override to admin channel
        if self.log_service is not None:
            msg = (
                f"[ADMIN OVERRIDE] @{admin_name} booked @{dj_name}"
                f" into {event_name} at {slot_display}"
            )
            await self.log_service.post_admin_override(msg)

        return True, note

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
            event_name: Notion event name (case-insensitive match).
            slot_num:   1-based slot number.

        Returns:
            (True, "") on success.
            (False, error_message) on failure or when no booking found.

        [Source: epics.md#Story 6.3 AC2, AC3]
        """
        if self.notion_client is None:
            return False, "Bot is still connecting to Notion — please try again."

        # Step 1: fetch future events
        try:
            events = await self.notion_client.poll_events()
        except Exception:
            logger.exception("admin cancel: poll_events failed")
            return False, "Could not fetch events from Notion — please try again."

        # Step 2: find matching event
        event, problem = self._find_event(events, event_name)
        if event is None:
            return False, problem
        event_name = self._get_event_name(event) or event_name

        event_id = event["id"]
        slot_index = slot_num - 1

        # Step 3: verify this DJ holds the slot (AC3)
        try:
            holder = await get_slot_lock_holder(self.db, event_id, slot_index)
        except Exception:
            logger.exception("admin cancel: get_slot_lock_holder failed")
            return False, "Database error checking slot — please try again."

        if holder != dj_id:
            return False, f"No booking found for @{dj_name} in that slot."

        # Step 4: compute slot unix
        slot_unix = self._get_slot_unix(event, slot_index)

        # Step 5: free the slot, but only if it is still this DJ's (it may have changed
        # hands since the check above)
        try:
            freed = await release_slot_lock(self.db, event_id, slot_index, dj_id)
        except Exception:
            logger.exception("admin cancel: release_slot_lock failed")
            return False, "Database error releasing slot — please try again."
        if not freed:
            return False, f"No booking found for @{dj_name} in that slot."

        # Step 6: unlink the DJ in Notion unless they keep another slot of this event
        # (tds layout: every slot has its own column, which is always cleared)
        notion_error: Exception | None = None
        try:
            if self.notion_client.per_slot is True or not await holds_slot_in_event(
                self.db, event_id, dj_id
            ):
                await self.notion_client.delete_booking(
                    event_id,
                    await get_notion_dj_page_id(self.db, dj_id),
                    [],
                    event_name,
                    slot_unix,
                    slot_index=slot_index,
                    dj_name=await self._cell_name(dj_id),
                )
        except Exception as exc:
            logger.error("admin cancel: Notion unlink failed: %s", exc)
            notion_error = exc

        # Step 7: DM the DJ (non-fatal)
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "your slot"
        await self._dm_dj(
            dj_id,
            f"An organizer has cancelled your booking for {event_name} at {slot_display}.",
        )

        # Step 8: log override to admin channel
        if self.log_service is not None:
            msg = (
                f"[ADMIN OVERRIDE] @{admin_name} cancelled @{dj_name}"
                f" from {event_name} at {slot_display}"
            )
            await self.log_service.post_admin_override(msg)

        if notion_error is not None:
            return False, (
                "Slot released, but Notion could not be updated — "
                "please unlink the DJ there by hand."
            )
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


def _parse_day(text: str) -> datetime.date | None:
    """A date given as YYYY-MM-DD or DD.MM.YYYY, else None."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _event_day(event: dict) -> datetime.date | None:
    """The calendar day of an event's Notion Date, or None."""
    try:
        return datetime.date.fromisoformat(event["properties"]["Date"]["date"]["start"][:10])
    except (KeyError, TypeError, ValueError):
        return None
