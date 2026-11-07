"""BotContext — shared dataclass passed to all Cogs and Services.

Holds: DB pool, Notion client, config, AdminLogService, and per-user asyncio.Locks.
Every multi-step DM flow acquires the user's lock on entry via get_user_lock().
[Source: architecture.md#Inter-Component Communication: Direct Calls via Shared BotContext]
[Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
"""
import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from bot.config import Config
    from bot.log import AdminLogService


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
    dj_conversations: "Any | None" = None
    admins: "Any | None" = None
    user_locks: dict[int, asyncio.Lock] = field(default_factory=dict)

    def is_admin(self, user_id: int) -> bool:
        """Registry (owners plus added admins) if set, else the env admin ids."""
        if self.admins is not None:
            return self.admins.is_admin(user_id)
        return user_id in self.config.admin_user_ids

    def admin_ids(self) -> list[int]:
        if self.admins is not None:
            return self.admins.all_ids()
        return sorted(self.config.admin_user_ids)

    def get_user_lock(self, user_id: int) -> asyncio.Lock:
        """Get or create the asyncio.Lock for this user.

        Called at the entry point of every multi-step DM flow (onboarding,
        booking, cancellation). If the lock is already held, the flow handler
        should respond with 'flow already open' and return immediately.

        The lock must be released on: completion, abandonment, timeout, or error.
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()
        return self.user_locks[user_id]
