"""AdminManagementService: add and remove admins, shared by the slash commands and the
admin page.

The registry writes the database before memory, so a failed save changes nothing. The
admin log entries are best effort: they are posted only while the bot is active and a
failing post never fails or undoes the change.
"""
import logging
import re
from dataclasses import dataclass
from typing import Any

import disnake

from bot.context import BotContext
from bot.services.activation import bot_is_active
from bot.services.admin_registry import AdminRegistry

logger = logging.getLogger(__name__)

_MAX_NAME = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
SAVE_FAILED = "Could not save the change. Try again."
ALREADY_ADMIN = "This person is already an admin."
NOT_FOUND = "No Discord user with this ID."
DISCORD_UNREACHABLE = "Could not reach Discord. Try again."
IS_BOT = "Bots cannot be admins."
IS_OWNER = "Owners can only be changed with ADMIN_USER_IDS."
NOT_ADDED = "This person is not an added admin."


@dataclass(frozen=True)
class AdminChange:
    ok: bool
    message: str
    name: str | None = None  # the person's display name, set on success


def clean_name(name: str | None) -> str | None:
    """A display name without control and direction characters, at most 80 characters."""
    if not name:
        return None
    cleaned = _CONTROL.sub("", name).strip()[:_MAX_NAME].strip()
    return cleaned or None


def _user_name(user: Any) -> str | None:
    return clean_name(getattr(user, "global_name", None)) or clean_name(
        getattr(user, "name", None)
    )


class AdminManagementService:
    def __init__(self, ctx: BotContext, bot: Any) -> None:
        if ctx.admins is None:
            raise RuntimeError("Admin management needs the admin registry")
        self._ctx = ctx
        self._registry: AdminRegistry = ctx.admins
        self._bot = bot

    async def add(self, user_id: int, actor: str | None) -> AdminChange:
        registry = self._registry
        await registry.ensure_loaded()
        if registry.is_admin(user_id):
            return AdminChange(False, ALREADY_ADMIN)
        try:
            user = await self._bot.fetch_user(user_id)
        except disnake.NotFound:
            return AdminChange(False, NOT_FOUND)
        except Exception:
            logger.warning("Discord lookup failed while adding an admin", exc_info=True)
            return AdminChange(False, DISCORD_UNREACHABLE)
        if getattr(user, "bot", False):
            return AdminChange(False, IS_BOT)
        try:
            added = await registry.add(user_id)
        except Exception:
            logger.warning("Could not save the new admin", exc_info=True)
            return AdminChange(False, SAVE_FAILED)
        if not added:
            return AdminChange(False, ALREADY_ADMIN)
        name = _user_name(user) or str(user_id)
        await self._log("log_admin_added", name, user_id, actor)
        return AdminChange(True, f"Added {name}.", name)

    async def remove(self, user_id: int, actor: str | None) -> AdminChange:
        registry = self._registry
        await registry.ensure_loaded()
        if registry.is_owner(user_id):
            return AdminChange(False, IS_OWNER)
        if user_id not in registry.added:
            return AdminChange(False, NOT_ADDED)
        name = await self.display_name(user_id) or str(user_id)
        try:
            removed = await registry.remove(user_id)
        except Exception:
            logger.warning("Could not save the admin removal", exc_info=True)
            return AdminChange(False, SAVE_FAILED)
        if not removed:
            return AdminChange(False, NOT_ADDED)
        await self._log("log_admin_removed", name, user_id, actor)
        return AdminChange(True, f"Removed {name}.", name)

    async def list_admins(self) -> list[tuple[int, str | None, bool]]:
        """(user id, display name or None, is owner): owners first, then added admins."""
        registry = self._registry
        await registry.ensure_loaded()
        return [
            (uid, await self.display_name(uid), registry.is_owner(uid))
            for uid in registry.all_ids()
        ]

    async def display_name(self, user_id: int) -> str | None:
        """Cached member or user first, then a Discord lookup; None when unknown."""
        try:
            bot = self._bot
            guild = bot.guilds[0] if bot.guilds else None
            member = guild.get_member(user_id) if guild is not None else None
            if member is not None:
                return clean_name(member.display_name)
            cached = bot.get_user(user_id)
            if cached is not None:
                name = _user_name(cached)
                if name:
                    return name
            return _user_name(await bot.fetch_user(user_id))
        except Exception:
            logger.warning("Could not look up a Discord user name", exc_info=True)
            return None

    async def _log(self, method: str, name: str, user_id: int, actor: str | None) -> None:
        try:
            service = self._ctx.log_service
            if service is not None and await bot_is_active(self._bot, self._ctx.db):
                await getattr(service, method)(name, user_id, actor)
        except Exception:
            logger.warning("Could not post the %s log entry", method, exc_info=True)
