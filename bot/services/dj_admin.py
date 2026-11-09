"""Admin management of the DJ list: add, edit and delete DJs (guests included).

Used by the admin page and the /dj commands. A guest is a DJ without a Discord
account. The djs table is keyed by Discord user id, so a guest gets a negative id
made up by the bot (see next_guest_id); Discord ids are positive. The bot never
contacts guests (no invites, reminders or booking notices).

Every method returns a DjResult and never raises for expected problems.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from bot.db.bookings import get_dj_slot_locks
from bot.db.djs import (
    delete_dj,
    dj_name_exists,
    get_all_dj_rows,
    get_dj_row,
    get_notion_dj_page_id,
    insert_dj,
    next_guest_id,
    update_dj_fields,
)
from bot.services.dj_sync import sync_dj
from bot.services.dj_validation import (
    MAX_LINK_LENGTH,
    MAX_NAME_LENGTH,
    MAX_VRCHAT_LENGTH,
    clean_text,
    name_error,
    normalize_link,
)
from bot.services.logo import LogoError, process_logo_async

if TYPE_CHECKING:
    from bot.context import BotContext
    from bot.notion.models import EventInfo

logger = logging.getLogger(__name__)

GUEST_NAME = "Guest (no Discord)"
NOT_READY = "The bot is not ready yet. Please try again in a moment."
UNKNOWN_DJ = "There is no DJ with this name."
_LOGO_TEXT = {
    "too_big": "The logo is too big. Use a PNG or JPEG under 5 MB.",
    "not_image": "The logo is not a usable image. Use a PNG or JPEG under 5 MB.",
}
_LOGO_DEFAULT = "The logo could not be read. Use a PNG or JPEG under 5 MB."


def _duplicate_text(exc: asyncpg.UniqueViolationError, name: str) -> str:
    if getattr(exc, "constraint_name", None) == "ux_djs_dj_name_norm":
        return f'There is already a DJ called "{name}".'
    return "This DJ already exists."


@dataclass
class DjResult:
    ok: bool
    message: str


def is_guest(dj_id: int) -> bool:
    return dj_id < 0


def parse_discord_id(raw: str | None) -> int | None:
    """A Discord user id typed by an admin, or None when it is not one."""
    text = (raw or "").strip()
    if not (text.isascii() and text.isdigit() and 17 <= len(text) <= 20):
        return None
    value = int(text)
    return value if value < 2**63 else None


def check_link(raw: str, label: str) -> tuple[str | None, str | None]:
    """(clean link, None) or (None, error). Only http(s) links with a host."""
    link = normalize_link(raw)
    scheme, _, rest = link.partition("://")
    if scheme.lower() not in ("http", "https") or not rest.strip("/"):
        return None, f"The {label} must be a web link (https://...)."
    if len(link) > MAX_LINK_LENGTH:
        return None, f"The {label} is too long."
    return link, None


class DjAdminService:
    def __init__(self, ctx: "BotContext", bot: Any) -> None:
        self._ctx = ctx
        self._bot = bot
        self._lock = asyncio.Lock()  # one add/edit at a time: name checks and guest ids

    # --- reading ---

    async def list_djs(self) -> list[dict]:
        if self._ctx.db is None:
            return []
        return await get_all_dj_rows(self._ctx.db)

    async def get_dj(self, dj_id: int) -> dict | None:
        if self._ctx.db is None:
            return None
        return await get_dj_row(self._ctx.db, dj_id)

    async def find_by_name(self, name: str) -> dict | None:
        wanted = clean_text(name).lower()
        for row in await self.list_djs():
            if clean_text(row["dj_name"]).lower() == wanted:
                return row
        return None

    async def future_bookings(self, dj_id: int) -> "list[tuple[EventInfo, int]]":
        """The DJ's bookings in events that are still upcoming: (event, slot_index)."""
        db, notion = self._ctx.db, self._ctx.notion_client
        if db is None or notion is None:
            return []
        locks = await get_dj_slot_locks(db, dj_id)
        if not locks:
            return []
        events = {e.page_id: e for e in await notion.list_events()}
        found = [(events[x["event_id"]], x["slot_index"]) for x in locks if x["event_id"] in events]
        return sorted(found, key=lambda item: (item[0].date, item[1]))

    # --- validation ---

    @staticmethod
    async def _logo(data: bytes | None) -> tuple[bytes | None, str | None]:
        if not data:
            return None, None
        try:
            return await process_logo_async(data), None
        except LogoError as exc:
            return None, _LOGO_TEXT.get(exc.reason, _LOGO_DEFAULT)

    @staticmethod
    def _name(raw: str) -> tuple[str | None, str | None]:
        problem = name_error(raw)
        if problem == "empty":
            return None, "The DJ name is required."
        if problem == "too_long":
            return None, f"The DJ name is too long (at most {MAX_NAME_LENGTH} characters)."
        return clean_text(raw), None

    @staticmethod
    def _vrchat(raw: str | None) -> tuple[str | None, str | None]:
        text = clean_text(raw or "")
        if len(text) > MAX_VRCHAT_LENGTH:
            return None, f"The VRChat name is too long (at most {MAX_VRCHAT_LENGTH} characters)."
        return (text or None), None

    @staticmethod
    def _optional_link(raw: str | None) -> tuple[str | None, str | None]:
        if not (raw or "").strip():
            return None, None
        return check_link(raw or "", "backup link")

    # --- add ---

    async def add_dj(
        self,
        *,
        admin_name: str,
        name: str,
        link: str,
        alt_link: str | None = None,
        vrchat: str | None = None,
        discord_id: int | None = None,
        logo: bytes | None = None,
    ) -> DjResult:
        db = self._ctx.db
        if db is None:
            return DjResult(False, NOT_READY)
        clean_name, err = self._name(name)
        if err:
            return DjResult(False, err)
        clean_link, err = check_link(link, "stream link")
        if err:
            return DjResult(False, err)
        clean_alt, err = self._optional_link(alt_link)
        if err:
            return DjResult(False, err)
        clean_vrchat, err = self._vrchat(vrchat)
        if err:
            return DjResult(False, err)
        image, err = await self._logo(logo)
        if err:
            return DjResult(False, err)
        assert clean_name is not None and clean_link is not None
        async with self._lock:
            if await dj_name_exists(db, clean_name):
                return DjResult(False, f'There is already a DJ called "{clean_name}".')
            if discord_id is not None and await get_dj_row(db, discord_id) is not None:
                return DjResult(False, "This Discord user is already a DJ.")
            dj_id = discord_id if discord_id is not None else await next_guest_id(db)
            try:
                await insert_dj(
                    db,
                    discord_user_id=dj_id,
                    dj_name=clean_name,
                    vrchat_name=clean_vrchat,
                    logo_url=None,
                    stream_url_primary=clean_link,
                    stream_url_backup=clean_alt,
                    logo_image=image,
                    active_booking=discord_id is not None,
                )
            except asyncpg.UniqueViolationError as exc:
                return DjResult(False, _duplicate_text(exc, clean_name))
        synced = await sync_dj(self._ctx, dj_id, self._discord_name(dj_id, clean_name))
        await self._log(admin_name, "added", clean_name)
        what = "guest DJ" if is_guest(dj_id) else "DJ"
        if not synced:
            return DjResult(
                True,
                f"Added the {what} {clean_name}, but Notion could not be updated. "
                "The Notion entry is created when the DJ is booked.",
            )
        return DjResult(True, f"Added the {what} {clean_name}.")

    # --- edit ---

    async def edit_dj(
        self,
        *,
        admin_name: str,
        dj_id: int,
        fields: dict[str, str | None],
        logo: bytes | None = None,
    ) -> DjResult:
        """Change the given fields. Keys: name, link, alt_link, vrchat.

        An empty alt_link or vrchat clears it; name and link cannot be empty.
        """
        db = self._ctx.db
        if db is None:
            return DjResult(False, NOT_READY)
        row = await get_dj_row(db, dj_id)
        if row is None:
            return DjResult(False, UNKNOWN_DJ)
        updates: dict[str, object] = {}
        if "name" in fields:
            clean_name, err = self._name(fields["name"] or "")
            if err:
                return DjResult(False, err)
            updates["dj_name"] = clean_name
        if "link" in fields:
            clean_link, err = check_link(fields["link"] or "", "stream link")
            if err:
                return DjResult(False, err)
            updates["stream_url_primary"] = clean_link
        if "alt_link" in fields:
            clean_alt, err = self._optional_link(fields["alt_link"])
            if err:
                return DjResult(False, err)
            updates["stream_url_backup"] = clean_alt
        if "vrchat" in fields:
            clean_vrchat, err = self._vrchat(fields["vrchat"])
            if err:
                return DjResult(False, err)
            updates["vrchat_name"] = clean_vrchat
        image, err = await self._logo(logo)
        if err:
            return DjResult(False, err)
        if image is not None:
            updates["logo_image"] = image
        if not updates:
            return DjResult(False, "Nothing to change.")
        async with self._lock:
            new_name = updates.get("dj_name")
            if isinstance(new_name, str) and await dj_name_exists(
                db, new_name, exclude_user_id=dj_id
            ):
                return DjResult(False, f'There is already a DJ called "{new_name}".')
            try:
                updated = await update_dj_fields(db, dj_id, **updates)
            except asyncpg.UniqueViolationError as exc:
                return DjResult(False, _duplicate_text(exc, str(new_name)))
            if not updated:
                return DjResult(False, UNKNOWN_DJ)
        fresh = await get_dj_row(db, dj_id)
        name = fresh["dj_name"] if fresh is not None else row["dj_name"]
        synced = await sync_dj(self._ctx, dj_id, self._discord_name(dj_id, name))
        await self._log(admin_name, "edited", name)
        if not synced:
            return DjResult(
                True, f"Saved {name}, but Notion could not be updated. The bot tries again later."
            )
        return DjResult(True, f"Saved {name}.")

    # --- delete ---

    async def delete_dj(self, *, admin_name: str, dj_id: int) -> DjResult:
        """Delete a DJ and cancel their bookings in upcoming events.

        If a booking cannot be cancelled, nothing is deleted. Past bookings stay.
        """
        db, notion = self._ctx.db, self._ctx.notion_client
        booking = self._ctx.admin_booking_service
        if db is None or notion is None or booking is None:
            return DjResult(False, NOT_READY)
        row = await get_dj_row(db, dj_id)
        if row is None:
            return DjResult(False, UNKNOWN_DJ)
        name = row["dj_name"]
        # A DJ in the middle of booking or cancelling must not be deleted under it
        flow = None if is_guest(dj_id) else self._ctx.get_user_lock(dj_id)
        if flow is not None:
            if flow.locked():
                return DjResult(
                    False, f"{name} is booking or cancelling right now. Try again in a minute."
                )
            await flow.acquire()
        try:
            return await self._delete(admin_name, dj_id, name)
        finally:
            if flow is not None:
                flow.release()

    async def _delete(self, admin_name: str, dj_id: int, name: str) -> DjResult:
        db, notion = self._ctx.db, self._ctx.notion_client
        booking = self._ctx.admin_booking_service
        assert db is not None and notion is not None and booking is not None
        try:
            bookings = await self.future_bookings(dj_id)
        except Exception:
            logger.warning("Could not list the bookings of DJ %s", dj_id, exc_info=True)
            return DjResult(False, "Could not read the bookings from Notion. Try again.")
        for event, slot_index in bookings:
            ok, message = await booking.cancel_slot_override(
                admin_name=admin_name,
                dj_id=dj_id,
                dj_name=name,
                event_name=event.event_id,
                slot_num=slot_index + 1,
            )
            if not ok:
                return DjResult(
                    False,
                    f"Could not cancel {name} in {event.event_id}: {message} "
                    "The DJ was not deleted.",
                )
        try:
            page_id = await get_notion_dj_page_id(db, dj_id)
            if page_id:
                await notion.trash_dj_page(page_id)
        except Exception:
            logger.warning("Could not trash the Notion page of DJ %s", dj_id, exc_info=True)
            return DjResult(False, "Notion could not be updated. The DJ was not deleted.")
        try:
            await delete_dj(db, dj_id)
        except Exception:
            logger.exception("Deleting DJ %s failed", dj_id)
            return DjResult(
                False,
                "The database could not be updated. The DJ was not deleted; their Notion "
                "entry is in the Notion trash and comes back when you save the DJ again.",
            )
        await self._log(admin_name, "removed", name)
        count = len(bookings)
        extra = f" {count} upcoming booking{'s' if count != 1 else ''} cancelled." if count else ""
        return DjResult(True, f"Deleted {name}.{extra}")

    # --- helpers ---

    def _discord_name(self, dj_id: int, fallback: str) -> str:
        if is_guest(dj_id):
            return GUEST_NAME
        user = self._bot.get_user(dj_id) if self._bot is not None else None
        return getattr(user, "display_name", None) or fallback

    async def _log(self, admin_name: str, action: str, dj_name: str) -> None:
        service = self._ctx.log_service
        if service is None:
            return
        try:
            await service.log_dj_list_change(admin_name, action, dj_name)
        except Exception:
            logger.warning("Could not log the DJ list change", exc_info=True)
