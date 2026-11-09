"""BotContext — shared dataclass passed to all Cogs and Services.

Holds: DB pool, Notion client, config, AdminLogService, and per-user asyncio.Locks.
Every multi-step DM flow acquires the user's lock on entry via get_user_lock().
[Source: architecture.md#Inter-Component Communication: Direct Calls via Shared BotContext]
[Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
"""
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from bot.config import Config
    from bot.log import AdminLogService


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

    def release(self) -> None:
        """Free the lock, whoever holds it."""
        self._token = 0

    @property
    def token(self) -> int:
        """The current flow's token (0 when free)."""
        return self._token if self.locked() else 0

    def owns(self, token: int) -> bool:
        return token != 0 and self.locked() and self._token == token

    def release_if(self, token: int) -> bool:
        """Free the lock only if `token` is the current flow. True if it was freed."""
        if token != 0 and self._token == token:
            self._token = 0
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
        notion_sync_service: NotionSyncService that reconciles hand edits in Notion
                      with the Postgres slot locks; run by the EventsCog poll loop
                      before each lineup update (None until set in main.py).
        dj_conversations: DjConversationService for the DJ onboarding and profile
                      DM conversation (None until set in main.py).
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
    outreach_service: "Any | None" = None
    reminders_service: "Any | None" = None
    admin_booking_service: "Any | None" = None
    notion_sync_service: "Any | None" = None
    dj_admin_service: "Any | None" = None
    # Set while the stored Notion databases are gone and no new set was approved yet:
    # the reason, and the coroutine function the admin page calls to create a new set.
    notion_problem: str | None = None
    notion_recover: "Any | None" = None
    dj_conversations: "Any | None" = None
    admins: "Any | None" = None
    user_locks: dict[int, FlowLock] = field(default_factory=dict)

    def attach_notion(self, client: "Any") -> None:
        """Use a (new) Notion client everywhere, also in the services built without one."""
        self.notion_client = client
        for service in (
            self.lineup_service, self.notion_sync_service, self.outreach_service,
            self.reminders_service, self.admin_booking_service,
        ):
            if service is not None and hasattr(service, "notion_client"):
                service.notion_client = client

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
        return lock

    def _drop_free_locks(self) -> None:
        """Forget locks nobody holds, so the dict does not grow without bound."""
        for uid in [uid for uid, lock in self.user_locks.items() if not lock.locked()]:
            del self.user_locks[uid]
