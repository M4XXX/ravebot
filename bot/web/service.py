"""Data service for the admin page: admins, Notion recovery, backup, Notion sync.

Backup import, rebuild and the upgrade import retry are serialised by bot/maintenance.py
(R20); the functions they call refuse with an ActionResult while another one runs, so
no lock is added here. Every database read uses short pool calls; no connection is held
across a long operation.
"""
import datetime
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import disnake

from bot import maintenance as maintenance_ops
from bot.backup.export import export_database
from bot.backup.restore import import_database
from bot.context import BotContext
from bot.db import announcements as db_ann
from bot.db import config as config_db
from bot.db import events as db_events
from bot.db import issues, outbox
from bot.db.announcements import AnnouncementRule
from bot.db.issues import IssueRow
from bot.db.outbox import OutboxStats
from bot.notion.models import EventInfo
from bot.policy import BookingRules, current_rules, parse_rules_form, save_rules
from bot.results import ActionResult as ActionResult  # re-export; lives in bot.results
from bot.services import announcement_settings as ann_settings
from bot.services.admin_management import AdminManagementService, clean_name
from bot.services.admin_registry import AdminRegistry
from bot.services.announcement_settings import AnnouncementSettings
from bot.services.announcement_template import (
    DEFAULT_TEMPLATES,
    GENERAL,
    START,
    TemplateCheck,
    check_template,
)
from bot.services.dj_admin import parse_discord_id
from bot.services.lineup import upcoming_event
from bot.sync.cycle import CONFIG_GUILD_ID, LAST_ERROR_KEY, LAST_RUN_KEY
from bot.sync.notion_import import (
    PG_TRUTH_IMPORTED_KEY,
    post_upgrade_result,
    run_upgrade_import_if_needed,
)
from bot.sync.rebuild import (
    post_rebuild_result,
    rebuild_from_notion,
    run_startup_rebuild_if_needed,
    run_startup_recovery,
)
from bot.timezone import local_today
from bot.ui import texts
from bot.ui.setup_options import parse_hhmm
from bot.ui.setup_wizard import missing_permissions

logger = logging.getLogger(__name__)

_OK_TTL = 300.0
_FAIL_TTL = 30.0
ACTOR = "the admin page"
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
ANN_KINDS = (GENERAL, START)
ANN_MAX_DAYS = 365
ANN_SETTINGS_SAVED = "The announcement settings are saved."
ANN_SETTINGS_RULE = (
    "The announcement settings are saved. The general announcement had no rules, so "
    "a default rule was created: 3 days before at 18:00."
)
ANN_RULE_ADDED = "The announcement rule was added."
ANN_RULE_UPDATED = "The announcement rule was updated."
ANN_RULE_REMOVED = "The announcement rule was removed."
ANN_RULE_NOT_FOUND = "This rule no longer exists."
ANN_CHANNEL_FORMAT = (
    "Enter the channel ID (17 to 20 digits), or leave it empty to clear the channel."
)
ANN_NO_TEXT_CHANNEL = "There is no text channel with this ID on the server."
ANN_UNKNOWN_KIND = "There is no announcement type with this name."
ANN_SAVE_FAILED = "The change could not be saved. Please try again."
ANN_EMPTY_WARNING = (
    "The text is empty: saving it resets to the default template. "
    "The preview shows the default template."
)
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
    ``can_retry`` is True while the startup import from Notion failed and Notion is
    configured, also when its databases could not be reached (no client attached).
    """

    enabled: bool
    last_run: datetime.datetime | None
    last_error: str | None
    outbox: OutboxStats
    issues: list[IssueRow]
    problem: str | None
    maintenance: str | None
    can_retry: bool = False


@dataclass(frozen=True)
class AnnouncementPageData:
    """What the Announcements page shows; ``event`` fills the preview (None: sample)."""

    settings: AnnouncementSettings
    rules: list[AnnouncementRule]
    event: EventInfo | None
    channel_name: str | None = None


def ann_template_saved(kind: str) -> str:
    return f"The {kind} template is saved."


def ann_template_reset(kind: str) -> str:
    return f"The {kind} template is back to the default."


def normalize_newlines(text: str) -> str:
    """Browser textareas post CRLF; store LF only. Nothing else is stripped."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_rule_form(raw: dict[str, str]) -> tuple[int, datetime.time] | str:
    """Days (0 to 365) and HH:MM, or the error text."""
    errors = []
    days_text = (raw.get("days") or "").strip()
    is_number = days_text.isascii() and days_text.isdigit() and len(days_text) <= 3
    days = int(days_text) if is_number else -1
    if not 0 <= days <= ANN_MAX_DAYS:
        errors.append(texts.ANNOUNCE_RULE_DAYS_INVALID)
    clean = parse_hhmm(raw.get("time") or "")
    if clean is None:
        errors.append(texts.ANNOUNCE_RULE_TIME_INVALID)
    if errors:
        return " ".join(errors)
    assert clean is not None
    return days, datetime.time(int(clean[:2]), int(clean[3:]))


def _merge_checks(real: TemplateCheck, sample: TemplateCheck) -> TemplateCheck:
    """The check with the next event, plus the problems only the sample event has."""
    extra = [f"With the sample event: {p}" for p in sample.problems if p not in real.problems]
    warnings = real.warnings + [w for w in sample.warnings if w not in real.warnings]
    problems = real.problems + extra
    return TemplateCheck(not problems, problems, warnings, real.length, real.preview)


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
        self._management = AdminManagementService(ctx, bot)
        self._bot = bot
        self._clock = clock
        self._cache: dict[int, tuple[float, UserInfo]] = {}

    def notion_problem(self) -> str | None:
        """Why the Notion databases are unusable, or None when they are fine."""
        return self._ctx.notion_problem

    def maintenance(self) -> str | None:
        """The text for refused writes while an import replaces the data, else None."""
        return self._ctx.maintenance

    async def notion_import_pending(self) -> bool:
        """True while the one-time import from Notion after the update has not run."""
        db = self._ctx.db
        if db is None:
            return False
        try:
            return await config_db.get_value(db, CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY) != "1"
        except Exception:
            logger.exception("Reading the Notion import mark failed")
            return True

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
            "New Notion databases were created. Changes reach Notion again. The old "
            "data stays in the old databases.",
        )

    # --- data and backup, Notion sync ---

    def notion_enabled(self) -> bool:
        return self._ctx.notion_client is not None

    async def sync_status(self) -> SyncStatus:
        ctx = self._ctx
        running = maintenance_ops.running()
        busy = _OPERATIONS.get(running, ctx.maintenance) if running else ctx.maintenance
        can_retry = bool(ctx.sync_problem) and ctx.db is not None and (
            self.notion_enabled() or getattr(ctx, "notion_connect", None) is not None
        )
        if not self.notion_enabled() or ctx.db is None:
            return SyncStatus(
                False, None, None, OutboxStats(0, None, None), [], ctx.sync_problem, busy,
                can_retry,
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
            can_retry=can_retry,
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
        connect = ctx.notion_connect
        if ctx.db is None or (ctx.notion_client is None and connect is None):
            return ActionResult(False, RETRY_NO_NOTION)
        if not ctx.sync_problem:
            return ActionResult(False, RETRY_NOTHING)
        if maintenance_ops.running():
            return ActionResult(False, texts.OPERATION_BUSY)
        if ctx.notion_client is None:
            # The databases could not be read at startup: try them again first.
            try:
                error = await connect()
            except Exception:
                logger.exception("Connecting to Notion for the import retry failed")
                return ActionResult(False, RETRY_ERROR)
            if error is not None or ctx.notion_client is None:
                return ActionResult(
                    False, RETRY_FAILED.format(problem=(error or RETRY_ERROR)[:300])
                )
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

    @property
    def schedule(self) -> Any:
        """The schedule page service (defaults, rules, event values, apply)."""
        from bot.web.schedule_page import SchedulePageService

        return SchedulePageService(self._ctx, self._bot)

    async def booking_rules(self) -> BookingRules:
        """The stored booking rules of the first server, or the defaults."""
        return await current_rules(self._ctx.db, self._bot)

    async def set_booking_rules(self, raw: dict[str, str]) -> ActionResult:
        """Validate the form values and store the rules; errors come back as the message."""
        rules, errors = parse_rules_form(raw)
        if rules is None:
            return ActionResult(False, " ".join(errors))
        if not self._bot.guilds or self._ctx.db is None:
            return ActionResult(False, texts.SCHEDULE_NOT_READY)
        try:
            await save_rules(self._ctx.db, self._bot.guilds[0].id, rules)
        except Exception:
            logger.exception("Could not save the booking rules")
            return ActionResult(False, "The booking rules could not be saved. Please try again.")
        return ActionResult(True, "Booking rules saved.")

    # --- event announcements ---

    def _ann_target(self) -> tuple[Any, int] | None:
        """(db, guild id) of the first server, or None while the bot is not ready."""
        if not self._bot.guilds or self._ctx.db is None:
            return None
        return self._ctx.db, self._bot.guilds[0].id

    async def _next_event(self) -> EventInfo | None:
        """The next upcoming event that is not finished, for the check and the preview."""
        db = self._ctx.db
        if db is None:
            return None
        try:
            return upcoming_event(await db_events.list_events(db), local_today())
        except Exception:
            logger.warning("Could not read the events for the template preview", exc_info=True)
            return None

    def _channel_name(self, channel_id: int | None) -> str | None:
        if channel_id is None or not self._bot.guilds:
            return None
        channel = self._bot.guilds[0].get_channel(channel_id)
        name = getattr(channel, "name", None)
        return name if isinstance(name, str) else None

    async def announcements(self) -> AnnouncementPageData | None:
        """Settings, rules and the preview event, or None while the bot is not ready."""
        target = self._ann_target()
        if target is None:
            return None
        db, guild_id = target
        current = await ann_settings.load_settings(db, guild_id)
        return AnnouncementPageData(
            settings=current,
            rules=await db_ann.list_rules(db, guild_id),
            event=await self._next_event(),
            channel_name=self._channel_name(current.channel_id),
        )

    def _check_channel(self, raw: str, stored: int | None = None) -> int | None | str:
        """The channel id (None clears it), or the error text.

        The channel the settings already hold is not checked for permissions again, so a
        switch can still be turned off after that channel is gone.
        """
        text = raw.strip()
        if not text:
            return None
        channel_id = parse_discord_id(text)
        if channel_id is None:
            return ANN_CHANNEL_FORMAT
        if channel_id == stored:
            return channel_id
        guild = self._bot.guilds[0]
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, disnake.TextChannel):
            return ANN_NO_TEXT_CHANNEL
        missing = missing_permissions(channel, guild.me)
        if missing:
            return texts.announce_channel_unusable(f"#{channel.name}", missing)
        return channel_id

    async def save_announcement_settings(self, raw: dict[str, str]) -> ActionResult:
        """Channel id (empty clears it, absent keeps it) and the two switches (a key
        present means on).

        ``set_enabled`` runs only when a switch changes, so a repeated "on" keeps the
        moment the general announcement was switched on.
        """
        target = self._ann_target()
        if target is None:
            return ActionResult(False, texts.SCHEDULE_NOT_READY)
        db, guild_id = target
        try:
            current = await ann_settings.load_settings(db, guild_id)
        except Exception:
            logger.exception("Could not save the announcement settings")
            return ActionResult(False, ANN_SAVE_FAILED)
        channel = self._check_channel(raw.get("channel") or "", current.channel_id)
        if isinstance(channel, str):
            return ActionResult(False, channel)
        wanted = {GENERAL: GENERAL in raw, START: START in raw}
        try:
            if "channel" in raw and channel != current.channel_id:
                await ann_settings.set_channel(db, guild_id, channel)
            stored = {GENERAL: current.general_enabled, START: current.start_enabled}
            rule_created = False
            for kind in ANN_KINDS:
                if wanted[kind] == stored[kind]:
                    continue
                had_rules = bool(await db_ann.list_rules(db, guild_id)) if wanted[kind] else True
                await ann_settings.set_enabled(db, guild_id, kind, wanted[kind])
                if kind == GENERAL and not had_rules:
                    rule_created = bool(await db_ann.list_rules(db, guild_id))
        except Exception:
            logger.exception("Could not save the announcement settings")
            return ActionResult(False, ANN_SAVE_FAILED)
        return ActionResult(True, ANN_SETTINGS_RULE if rule_created else ANN_SETTINGS_SAVED)

    async def get_announcement_rule(self, rule_id: int) -> AnnouncementRule | None:
        target = self._ann_target()
        if target is None:
            return None
        rules = await db_ann.list_rules(*target)
        return next((r for r in rules if r.id == rule_id), None)

    async def add_rule(self, raw: dict[str, str]) -> ActionResult:
        parsed = _parse_rule_form(raw)
        if isinstance(parsed, str):
            return ActionResult(False, parsed)
        target = self._ann_target()
        if target is None:
            return ActionResult(False, texts.SCHEDULE_NOT_READY)
        try:
            await db_ann.add_rule(*target, *parsed)
        except Exception:
            logger.exception("Could not add an announcement rule")
            return ActionResult(False, ANN_SAVE_FAILED)
        return ActionResult(True, ANN_RULE_ADDED)

    async def edit_rule(self, rule_id: int, raw: dict[str, str]) -> ActionResult:
        parsed = _parse_rule_form(raw)
        if isinstance(parsed, str):
            return ActionResult(False, parsed)
        target = self._ann_target()
        if target is None:
            return ActionResult(False, texts.SCHEDULE_NOT_READY)
        try:
            saved = await db_ann.update_rule(*target, rule_id, *parsed)
        except Exception:
            logger.exception("Could not change an announcement rule")
            return ActionResult(False, ANN_SAVE_FAILED)
        if saved is None:
            return ActionResult(False, ANN_RULE_NOT_FOUND)
        return ActionResult(True, ANN_RULE_UPDATED)

    async def remove_rule(self, rule_id: int) -> ActionResult:
        target = self._ann_target()
        if target is None:
            return ActionResult(False, texts.SCHEDULE_NOT_READY)
        try:
            removed = await db_ann.delete_rule(*target, rule_id)
        except Exception:
            logger.exception("Could not remove an announcement rule")
            return ActionResult(False, ANN_SAVE_FAILED)
        if not removed:
            return ActionResult(False, ANN_RULE_NOT_FOUND)
        return ActionResult(True, ANN_RULE_REMOVED)

    async def check_template(self, kind: str, text: str) -> TemplateCheck:
        """The same check a save runs, with the next event (or the sample) as preview."""
        if kind not in ANN_KINDS:
            raise ValueError(f"Unknown announcement kind: {kind!r}")
        text = normalize_newlines(text)
        event = await self._next_event()
        if not text.strip():
            check = check_template(DEFAULT_TEMPLATES[kind], kind, event)
            return TemplateCheck(
                check.ok, check.problems, [ANN_EMPTY_WARNING, *check.warnings],
                check.length, check.preview,
            )
        sample = check_template(text, kind, None)
        if event is None:
            return sample
        return _merge_checks(check_template(text, kind, event), sample)

    async def save_template(self, kind: str, text: str) -> ActionResult:
        """Check and store a template; an invalid one is refused, empty text resets it."""
        if kind not in ANN_KINDS:
            return ActionResult(False, ANN_UNKNOWN_KIND)
        target = self._ann_target()
        if target is None:
            return ActionResult(False, texts.SCHEDULE_NOT_READY)
        text = normalize_newlines(text)
        event = await self._next_event()
        try:
            check = await ann_settings.save_template(*target, kind, text, event=event)
        except Exception:
            logger.exception("Could not save the %s announcement template", kind)
            return ActionResult(False, ANN_SAVE_FAILED)
        if not check.ok:
            return ActionResult(False, "The template was not saved: " + " ".join(check.problems))
        if not text.strip():
            return ActionResult(True, ann_template_reset(kind))
        message = ann_template_saved(kind)
        if check.warnings:
            message += " " + " ".join(check.warnings)
        return ActionResult(True, message)

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
                    clean_name(member.display_name),
                    _avatar_url(member),
                    True,
                )
        user = await self._bot.fetch_user(user_id)
        name = clean_name(getattr(user, "global_name", None)) or clean_name(user.name)
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
        await self._registry.ensure_loaded()
        user_id = _parse_id(raw_id)
        if user_id is None:
            return ActionResult(False, FORMAT_MESSAGE)
        change = await self._management.add(user_id, ACTOR)
        return ActionResult(change.ok, change.message)

    async def remove_admin(self, raw_id: str) -> ActionResult:
        await self._registry.ensure_loaded()
        user_id = _parse_id(raw_id)
        if user_id is None:
            return ActionResult(False, FORMAT_MESSAGE)
        change = await self._management.remove(user_id, ACTOR)
        return ActionResult(change.ok, change.message)
