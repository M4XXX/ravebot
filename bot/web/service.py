"""Data service for the admin page: describing people, adding and removing admins."""
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import disnake

from bot.context import BotContext
from bot.services.activation import bot_is_active
from bot.services.admin_registry import AdminRegistry
from bot.services.dj_admin import parse_discord_id

logger = logging.getLogger(__name__)

_OK_TTL = 300.0
_FAIL_TTL = 30.0
_MAX_NAME = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
SAVE_FAILED = "Could not save the change. Try again."
FORMAT_MESSAGE = "Please enter a Discord user ID (17 to 20 digits)."


@dataclass(frozen=True)
class UserInfo:
    user_id: int
    name: str | None
    avatar_url: str | None
    on_server: bool


@dataclass(frozen=True)
class ServerInfo:
    guild_id: int
    name: str
    icon_url: str | None


@dataclass(frozen=True)
class AdminRow:
    info: UserInfo
    is_owner: bool


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = _CONTROL.sub("", name).strip()[:_MAX_NAME].strip()
    return cleaned or None


def _parse_id(raw: str) -> int | None:
    return parse_discord_id(raw)


def _avatar_url(obj: Any) -> str | None:
    avatar = getattr(obj, "display_avatar", None)
    return avatar.with_size(64).url if avatar is not None else None


class AdminPageService:
    def __init__(
        self, ctx: BotContext, bot: Any, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if ctx.admins is None:
            raise RuntimeError("The admin page needs the admin registry")
        self._ctx = ctx
        self._registry: AdminRegistry = ctx.admins
        self._bot = bot
        self._clock = clock
        self._cache: dict[int, tuple[float, UserInfo]] = {}

    @property
    def djs(self) -> Any:
        """The DJ list service (add, edit, delete)."""
        if self._ctx.dj_admin_service is None:
            raise RuntimeError("The DJ list service is not ready")
        return self._ctx.dj_admin_service

    def server_info(self) -> ServerInfo | None:
        if not self._bot.guilds:
            return None
        guild = self._bot.guilds[0]
        icon = getattr(guild, "icon", None)
        return ServerInfo(guild.id, guild.name, icon.url if icon else None)

    async def describe(self, user_id: int) -> UserInfo:
        cached = self._cache.get(user_id)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        try:
            info = await self._lookup(user_id)
            ttl = _OK_TTL
        except Exception:
            logger.warning("Could not look up a Discord user for the admin page", exc_info=True)
            info = UserInfo(user_id, None, None, False)
            ttl = _FAIL_TTL
        self._cache[user_id] = (self._clock() + ttl, info)
        return info

    async def _lookup(self, user_id: int) -> UserInfo:
        guild = self._bot.guilds[0] if self._bot.guilds else None
        if guild is not None:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (disnake.NotFound, disnake.HTTPException):
                    member = None
            if member is not None:
                return UserInfo(
                    user_id,
                    _clean_name(member.display_name),
                    _avatar_url(member),
                    True,
                )
        user = await self._bot.fetch_user(user_id)
        name = _clean_name(getattr(user, "global_name", None)) or _clean_name(user.name)
        return UserInfo(user_id, name, _avatar_url(user), False)

    async def list_admins(self) -> list[AdminRow]:
        registry = self._registry
        await registry.ensure_loaded()
        return [
            AdminRow(await self.describe(uid), registry.is_owner(uid))
            for uid in self._ctx.admin_ids()
        ]

    async def admin_row(self, raw_id: str) -> AdminRow | None:
        """The row for a valid id that is an owner or an added admin, else None."""
        try:
            user_id = _parse_id(raw_id)
            if user_id is None:
                return None
            registry = self._registry
            await registry.ensure_loaded()
            is_owner = registry.is_owner(user_id)
            if not is_owner and user_id not in registry.added:
                return None
            return AdminRow(await self.describe(user_id), is_owner)
        except Exception:
            logger.warning("Could not look up an admin for the admin page", exc_info=True)
            return None

    async def add_admin(self, raw_id: str) -> ActionResult:
        registry = self._registry
        await registry.ensure_loaded()
        user_id = _parse_id(raw_id)
        if user_id is None:
            return ActionResult(False, FORMAT_MESSAGE)
        if registry.is_admin(user_id):
            return ActionResult(False, "This person is already an admin.")
        try:
            user = await self._bot.fetch_user(user_id)
        except disnake.NotFound:
            return ActionResult(False, "No Discord user with this ID.")
        except Exception:
            logger.warning("Discord lookup failed while adding an admin", exc_info=True)
            return ActionResult(False, "Could not reach Discord. Try again.")
        try:
            added = await registry.add(user_id)
        except Exception:
            logger.warning("Could not save the new admin", exc_info=True)
            return ActionResult(False, SAVE_FAILED)
        if not added:
            return ActionResult(False, "This person is already an admin.")
        name = _clean_name(getattr(user, "global_name", None)) or _clean_name(user.name)
        name = name or str(user_id)
        await self._log_added(name, user_id)
        return ActionResult(True, f"Added {name}.")

    async def _log_added(self, name: str, user_id: int) -> None:
        try:
            service = self._ctx.log_service
            if service is not None and await bot_is_active(self._bot, self._ctx.db):
                await service.log_admin_added(name, user_id)
        except Exception:
            logger.warning("Could not post the admin added log entry", exc_info=True)

    async def remove_admin(self, raw_id: str) -> ActionResult:
        registry = self._registry
        await registry.ensure_loaded()
        user_id = _parse_id(raw_id)
        if user_id is None:
            return ActionResult(False, FORMAT_MESSAGE)
        if registry.is_owner(user_id):
            return ActionResult(False, "Owners can only be changed with ADMIN_USER_IDS.")
        if user_id not in registry.added:
            return ActionResult(False, "This person is not an added admin.")
        info = await self.describe(user_id)
        try:
            await registry.remove(user_id)
        except Exception:
            logger.warning("Could not save the admin removal", exc_info=True)
            return ActionResult(False, SAVE_FAILED)
        return ActionResult(True, f"Removed {info.name or user_id}.")
