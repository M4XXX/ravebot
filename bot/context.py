"""BotContext — shared dataclass passed to all Cogs and Services.

Holds: DB pool, Notion client, config, AdminLogService, and per-user asyncio.Locks.
Every multi-step DM flow acquires the user's lock on entry via get_user_lock().
[Source: architecture.md#Inter-Component Communication: Direct Calls via Shared BotContext]
[Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from bot.config import Config
    from bot.log import AdminLogService


logger = logging.getLogger(__name__)

# A DM flow left open this long counts as abandoned (views never time out).
STALE_FLOW_SECONDS = 15 * 60


class FlowLock(asyncio.Lock):
    """Per-user flow lock that frees itself once a flow was abandoned.

    DJ-facing views have timeout=None, so a DJ who closed a form or walked away kept the
    lock and the bot ignored them until a restart. locked() releases a lock held longer
    than STALE_FLOW_SECONDS and reports it free.
    """

    def __init__(self) -> None:
        super().__init__()
        self.acquired_at: float | None = None
        # Bumped on every acquire; views remember it, so menus of an older flow go inert.
        self.generation = 0

    async def acquire(self) -> bool:
        result = await super().acquire()
        self.acquired_at = time.monotonic()
        self.generation += 1
        return result

    def release(self) -> None:
        self.acquired_at = None
        super().release()

    def locked(self) -> bool:
        if (
            super().locked()
            and self.acquired_at is not None
            and time.monotonic() - self.acquired_at > STALE_FLOW_SECONDS
        ):
            logger.info("Releasing a flow lock held for over %d s", STALE_FLOW_SECONDS)
            self.release()
        return super().locked()

    def release_if_held(self) -> None:
        """Release without raising when nothing holds the lock (safe on every exit path)."""
        if super().locked():
            self.release()

    def release_if_generation(self, generation: int | None) -> None:
        """Release only while the flow that took the lock as `generation` still holds it.

        A form or menu of an older flow must not free the lock of the DJ's current flow.
        """
        if generation is not None and self.generation == generation and super().locked():
            self.release()


@dataclass
class BotContext:
    """Shared application context passed to all Cogs and Services.

    Fields:
        config:       Immutable configuration loaded from env vars.
        db:           asyncpg connection pool (set in Story 1.2, None until then).
        notion_client: Notion REST API client (set in Story 3.x, None until then).
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
    intervention_service: "Any | None" = None
    admin_booking_service: "Any | None" = None
    user_locks: dict[int, FlowLock] = field(default_factory=dict)

    def get_user_lock(self, user_id: int) -> FlowLock:
        """Get or create the asyncio.Lock for this user.

        Called at the entry point of every multi-step DM flow (onboarding,
        booking, cancellation). If the lock is already held, the flow handler
        should respond with 'flow already open' and return immediately.

        The lock must be released on: completion, abandonment, timeout, or error.
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        if user_id not in self.user_locks:
            self.user_locks[user_id] = FlowLock()
        return self.user_locks[user_id]
