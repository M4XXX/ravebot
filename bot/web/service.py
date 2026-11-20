"""Data service for the admin page: admins, Notion recovery, backup, Notion sync.

Backup import, rebuild and the upgrade import retry are serialised by bot/maintenance.py
(R20); the functions they call refuse with an ActionResult while another one runs, so
no lock is added here. Every database read uses short pool calls; no connection is held
across a long operation.
"""
import datetime
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import disnake

from bot import maintenance as maintenance_ops
from bot.backup.export import export_database
from bot.backup.restore import import_database
from bot.context import BotContext
from bot.db import config as config_db
from bot.db import issues, outbox
from bot.db.issues import IssueRow
from bot.db.outbox import OutboxStats
from bot.results import ActionResult as ActionResult  # re-export; lives in bot.results
from bot.services.activation import bot_is_active
from bot.services.admin_registry import AdminRegistry
from bot.services.dj_admin import parse_discord_id
from bot.sync.cycle import CONFIG_GUILD_ID, LAST_ERROR_KEY, LAST_RUN_KEY
from bot.sync.notion_import import post_upgrade_result, run_upgrade_import_if_needed
from bot.sync.rebuild import (
    post_rebuild_result,
    rebuild_from_notion,
    run_startup_rebuild_if_needed,
    run_startup_recovery,
)
from bot.ui import texts

logger = logging.getLogger(__name__)

_OK_TTL = 300.0
_FAIL_TTL = 30.0
_MAX_NAME = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
SAVE_FAILED = "Could not save the change. Try again."
FORMAT_MESSAGE = "Please enter a Discord user ID (17 to 20 digits)."
ISSUE_CHOICES = ("postgres", "notion", "resynced")
SYNC_OFF = "The Notion sync is not running, so nothing was changed."
NO_ISSUE = "This conflict or warning is no longer open."
RESYNC_NOTHING = "The Notion mirror is not running, so nothing was queued."
IMPORT_FAILED = "The import failed. Nothing was changed. Check the bot log."
REBUILD_ERROR = "The rebuild failed. Check the bot log and the admin channel."
RETRY_NOTHING = "There is no failed import from Notion to retry."
RETRY_NO_NOTION = "Notion is not set up, so there is nothing to import."
RETRY_DONE = "The import from Notion worked. Booking and the Notion sync run again."
RETRY_FAILED = "The import from Notion failed again: {problem}"
RETRY_ERROR = "The import from Notion failed. Check the bot log."
_OPERATIONS = {
    "upgrade": "The import from Notion is running.",
    "rebuild": "A rebuild from Notion is running.",
    "import": "A backup import is running.",
}


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
class SyncStatus:
    """What the admin page shows about data operations and the Notion sync.

    ``enabled`` is True while a Notion client is attached (Notion configured and its
    databases usable); without it the page hides the Notion card (decision 10).
    ``maintenance`` names a running import or rebuild, or the pause text.
    """

    enabled: bool
    last_run: datetime.datetime | None
    last_error: str | None
    outbox: OutboxStats
    issues: list[IssueRow]
    problem: str | None
    maintenance: str | None


def _parse_time(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _parse_issue_id(raw: str) -> int | None:
    raw = raw.strip()
    if not raw.isdigit() or len(raw) > 18:
        return None
    return int(raw)


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

    def notion_problem(self) -> str | None:
        """Why the Notion databases are unusable, or None when they are fine."""
        return self._ctx.notion_problem

    def maintenance(self) -> str | None:
        """The text for refused writes while an import replaces the data, else None."""
        return self._ctx.maintenance

    async def recreate_notion(self) -> ActionResult:
        recover = self._ctx.notion_recover
        if self._ctx.notion_problem is None or recover is None:
            return ActionResult(False, "The Notion databases are fine. Nothing was changed.")
        error = await recover()
        if error is not None:
            return ActionResult(
                False,
                "Creating the new databases failed. Check that the Notion page is shared "
                f"with the integration, then try again. ({error[:200]})",
            )
        return ActionResult(
            True,
            "New Notion databases were created. Booking and the lineup work again. The old "
            "data stays in the old databases.",
        )

    # --- data and backup, Notion sync ---

    def notion_enabled(self) -> bool:
        return self._ctx.notion_client is not None

    async def sync_status(self) -> SyncStatus:
        ctx = self._ctx
        running = maintenance_ops.running()
        busy = _OPERATIONS.get(running, ctx.maintenance) if running else ctx.maintenance
        if not self.notion_enabled() or ctx.db is None:
            return SyncStatus(
                False, None, None, OutboxStats(0, None, None), [], ctx.sync_problem, busy
            )
        db = ctx.db
        last_run = await config_db.get_value(db, CONFIG_GUILD_ID, LAST_RUN_KEY)
        last_error = await config_db.get_value(db, CONFIG_GUILD_ID, LAST_ERROR_KEY)
        return SyncStatus(
            enabled=True,
            last_run=_parse_time(last_run),
            last_error=last_error or None,  # "" means the last cycle had no error
            outbox=await outbox.stats(db),
            issues=await issues.list_open(db),
            problem=ctx.sync_problem,
            maintenance=busy,
        )

    async def export_backup(self) -> bytes:
        """The gzip JSON export of the whole database (raises when it fails)."""
        return await export_database(self._ctx.db)

    async def import_backup(self, data: bytes) -> ActionResult:
        try:
            return await import_database(self._ctx, data)
        except Exception:
            logger.exception("The backup import from the admin page failed")
            return ActionResult(False, IMPORT_FAILED)

    async def rebuild_from_notion(self) -> ActionResult:
        try:
            return await rebuild_from_notion(self._ctx)
        except Exception:
            logger.exception("The rebuild from Notion from the admin page failed")
            return ActionResult(False, REBUILD_ERROR)

    async def open_issue(self, raw_id: str) -> IssueRow | None:
        issue_id = _parse_issue_id(raw_id)
        if issue_id is None or self._ctx.db is None:
            return None
        return next((i for i in await issues.list_open(self._ctx.db) if i.id == issue_id), None)

    async def resolve_issue(self, raw_id: str, choice: str) -> ActionResult:
        issue_id = _parse_issue_id(raw_id)
        if issue_id is None:
            return ActionResult(False, NO_ISSUE)
        if choice not in ISSUE_CHOICES:
            return ActionResult(False, texts.SYNC_WRONG_CHOICE)
        cycle = self._ctx.sync_cycle
        if cycle is None:
            return ActionResult(False, SYNC_OFF)
        return await cycle.resolve_issue(issue_id, choice)

    async def resync_all(self) -> ActionResult:
        ctx = self._ctx
        if ctx.maintenance or maintenance_ops.running():
            return ActionResult(False, texts.MAINTENANCE)
        if ctx.sync_cycle is None:
            return ActionResult(False, SYNC_OFF)
        count = await ctx.sync_cycle.resync_all()
        if count == 0:
            return ActionResult(False, RESYNC_NOTHING)
        return ActionResult(True, f"{count} records are queued and written to Notion again.")

    async def retry_import(self) -> ActionResult:
        """Run the startup import from Notion again after it failed; start the mirror.

        Same order as startup and ``recover_notion`` (bot/main.py): an empty Postgres is
        rebuilt (R19), else the upgrade import runs. When that ends the pause, the mirror
        and the sync cycle start through ``ctx.notion_start_mirror``, then the startup
        recovery checks run.
        """
        ctx = self._ctx
        if ctx.notion_client is None or ctx.db is None:
            return ActionResult(False, RETRY_NO_NOTION)
        if not ctx.sync_problem:
            return ActionResult(False, RETRY_NOTHING)
        if maintenance_ops.running():
            return ActionResult(False, texts.OPERATION_BUSY)
        try:
            if not await run_startup_rebuild_if_needed(ctx):
                await run_upgrade_import_if_needed(ctx)
            await post_upgrade_result(ctx)
            if ctx.maintenance is None and ctx.sync_problem is None:
                start = ctx.notion_start_mirror
                if start is not None:
                    await start()
                await run_startup_recovery(ctx)
            await post_rebuild_result(ctx)
        except Exception:
            logger.exception("Retrying the import from Notion failed")
            return ActionResult(False, RETRY_ERROR)
        if ctx.sync_problem:
            return ActionResult(False, RETRY_FAILED.format(problem=ctx.sync_problem[:300]))
        if ctx.maintenance:
            return ActionResult(False, RETRY_ERROR)
        return ActionResult(True, RETRY_DONE)

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
