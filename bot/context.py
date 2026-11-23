"""BotContext — shared dataclass passed to all Cogs and Services.

Holds: DB pool, Notion client, config, AdminLogService, and per-user asyncio.Locks.
Every multi-step DM flow acquires the user's lock on entry via get_user_lock().
[Source: architecture.md#Inter-Component Communication: Direct Calls via Shared BotContext]
[Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
"""
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from bot.config import Config
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

# An ephemeral Discord view stops working after 15 minutes (disnake forces this timeout),
# so a flow that has not finished by then was abandoned and its lock is free again.
FLOW_TTL_SECONDS = 15 * 60


class FlowLock:
    """Per-user lock for one multi-step DM flow (booking, cancelling, outreach accept).

    Same calls as asyncio.Lock (locked / acquire / release), plus:
    - every acquire starts a new flow with its own token; views keep the token and use
      release_if / owns, so a button of an old flow can never free or drive a newer one;
    - a flow older than FLOW_TTL_SECONDS counts as abandoned: locked() is False again.
      A dismissed message, a lost interaction or a crash in a view can therefore never
      block a user until the next restart.
    acquire never waits: callers check locked() first (no await in between).
    """

    def __init__(
        self, ttl: float = FLOW_TTL_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._token = 0  # 0 = free
        self._issued = 0
        self._since = 0.0
        # Called when a held lock is freed by release() or release_if(); the DM menu hook.
        self.on_release: Callable[[], None] | None = None

    def locked(self) -> bool:
        if self._token and self._clock() - self._since > self._ttl:
            self._token = 0  # abandoned flow
        return self._token != 0

    async def acquire(self) -> bool:
        if self.locked():
            raise RuntimeError("flow lock is already held")
        self._issued += 1
        self._token = self._issued
        self._since = self._clock()
        return True

    def _notify(self) -> None:
        if self.on_release is None:
            return
        try:
            self.on_release()
        except Exception:
            logger.warning("FlowLock: on_release failed", exc_info=True)

    def release(self) -> None:
        """Free the lock, whoever holds it. Fires on_release when it was held."""
        held = self._token != 0
        self._token = 0
        if held:
            self._notify()

    @property
    def token(self) -> int:
        """The current flow's token (0 when free)."""
        return self._token if self.locked() else 0

    def owns(self, token: int) -> bool:
        return token != 0 and self.locked() and self._token == token

    def release_if(self, token: int, notify: bool = True) -> bool:
        """Free the lock only if `token` is the current flow. True if it was freed.

        `notify` False frees it without firing on_release (an abandoned flow).
        """
        if token != 0 and self._token == token:
            self._token = 0
            if notify:
                self._notify()
            return True
        return False


@dataclass
class BotContext:
    """Shared application context passed to all Cogs and Services.

    Fields:
        config:       Immutable configuration loaded from env vars.
        db:           asyncpg connection pool (set in Story 1.2, None until then).
        notion_client: NotionClient for the Events, Slots and DJs databases (set in
                      main.py after ensure_schema/ensure_views, None until then).
        dj_conversations: DjConversationService for the DJ onboarding and profile
                      DM conversation (None until set in main.py).
        mirror:       MirrorWorker that pushes queued Postgres changes to Notion
                      (None while Notion is disabled or not set up).
        sync_cycle:   SyncCycle the Notion poll runs (read Notion, apply hand edits,
                      conflicts and warnings); None while Notion is disabled or not set up.
        maintenance:  Text for users while an import replaces the data; bookings and
                      edits are refused while it is set (bot/ui/gates.py).
        sync_problem: Last sync or import failure for the admins, or None.
        upgrade_import: Upgrade import result waiting to be posted in the admin log.
        rebuild_report: Rebuild-from-Notion result waiting to be posted in the admin log.
        admins:       AdminRegistry (owners plus added admins; None falls back to config).
        log_service:  AdminLogService (set in Story 1.3 main.py, None until then).
        user_locks:   Per-user asyncio.Lock dict — keyed by discord_user_id.
                      Prevents a DJ from running two concurrent DM flows simultaneously.
                      Note: grows by one entry per unique user; acceptable for a small
                      community bot. Replace with an LRU cache if user base grows large.
    """

    config: "Config"
    db: "asyncpg.Pool | None" = None
    notion_client: "Any | None" = None
    log_service: "AdminLogService | None" = None
    lineup_service: "Any | None" = None
    # ManagementLineupService: lineup messages in the DJ management channel.
    management_lineup_service: "Any | None" = None
    outreach_service: "Any | None" = None
    reminders_service: "Any | None" = None
    admin_booking_service: "Any | None" = None
    dj_admin_service: "Any | None" = None
    event_cancel_service: "Any | None" = None
    # ScheduleService: schedule rules, defaults and per-event genre/location/note.
    schedule_service: "Any | None" = None
    # Set while the stored Notion databases are gone and no new set was approved yet:
    # the reason, and the coroutine function the admin page calls to create a new set.
    notion_problem: str | None = None
    notion_recover: "Any | None" = None
    # Builds the Notion client again from the stored databases (lost share fixed);
    # returns an error text or None. None without Notion.
    notion_connect: "Any | None" = None
    # Starts the Notion mirror worker and sync cycle (main._start_mirror) once a rebuild
    # ended a pause; None without Notion.
    notion_start_mirror: "Any | None" = None
    # Set while an import replaces the data (the text users get), None otherwise.
    maintenance: str | None = None
    # The last sync or import failure shown to admins, None when there is none.
    sync_problem: str | None = None
    # The upgrade import result still to post in the admin log (posted once in on_ready).
    upgrade_import: "Any | None" = None
    # A startup rebuild result still to post in the admin log (posted once in on_ready).
    rebuild_report: "Any | None" = None
    dj_conversations: "Any | None" = None
    # MenuService: sends the DM menu again after a flow ended (None until set in main.py).
    menu_service: "Any | None" = None
    mirror: "Any | None" = None
    # SyncCycle run by the Notion poll (None while Notion is disabled or not set up).
    sync_cycle: "Any | None" = None
    admins: "Any | None" = None
    user_locks: dict[int, FlowLock] = field(default_factory=dict)

    def attach_notion(self, client: "Any") -> None:
        """Use a (new) Notion client, also in a mirror worker built without one.

        Only the mirror worker (and the import/poll code reading ctx.notion_client)
        talks to Notion; bookings, lineup, outreach and reminders do not.
        """
        self.notion_client = client
        if self.mirror is not None:
            self.mirror.notion_client = client

    def is_admin(self, user_id: int) -> bool:
        """Registry (owners plus added admins) if set, else the env admin ids."""
        if self.admins is not None:
            return self.admins.is_admin(user_id)
        return user_id in self.config.admin_user_ids

    def admin_ids(self) -> list[int]:
        if self.admins is not None:
            return self.admins.all_ids()
        return sorted(self.config.admin_user_ids)

    def get_user_lock(self, user_id: int) -> FlowLock:
        """Get or create the FlowLock for this user.

        Called at the entry point of every multi-step DM flow (onboarding,
        booking, cancellation). If the lock is already held, the flow handler
        should respond with 'flow already open' and return immediately.

        The lock must be released on: completion, abandonment, timeout, or error.
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        lock = self.user_locks.get(user_id)
        if lock is None:
            if len(self.user_locks) > 1000:
                self._drop_free_locks()
            lock = self.user_locks[user_id] = FlowLock()
            lock.on_release = lambda: self._menu_after_flow(user_id)
        return lock

    def _menu_after_flow(self, user_id: int) -> None:
        """A flow of this user ended: have the DM menu sent again."""
        if self.menu_service is not None:
            self.menu_service.schedule(user_id)

    def _drop_free_locks(self) -> None:
        """Forget locks nobody holds, so the dict does not grow without bound."""
        for uid in [uid for uid, lock in self.user_locks.items() if not lock.locked()]:
            del self.user_locks[uid]
