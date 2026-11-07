"""AdminRegistry: owners (environment) plus admins added at runtime (database)."""
import logging
from typing import Any

from bot.db.web_admins import add_web_admin, list_web_admins, remove_web_admin

logger = logging.getLogger(__name__)


class AdminRegistry:
    def __init__(self, owners: frozenset[int], db: Any) -> None:
        self._owners = frozenset(owners)
        self._db = db
        self._added: list[int] = []
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def owners(self) -> frozenset[int]:
        return self._owners

    @property
    def added(self) -> frozenset[int]:
        return frozenset(self._added)

    def is_owner(self, user_id: int) -> bool:
        return user_id in self._owners

    def is_admin(self, user_id: int) -> bool:
        return user_id in self._owners or user_id in self._added

    def all_ids(self) -> list[int]:
        return sorted(self._owners) + list(self._added)

    async def load(self) -> None:
        try:
            ids = await list_web_admins(self._db)
        except Exception:
            logger.warning("Could not load the added admins; owners only", exc_info=True)
            self._added = []
            return
        self._added = [i for i in dict.fromkeys(ids) if i not in self._owners]
        self._loaded = True

    async def ensure_loaded(self) -> None:
        """Retry load() while it has never succeeded. Never raises."""
        if not self._loaded:
            await self.load()

    async def add(self, user_id: int) -> bool:
        if user_id in self._owners or user_id in self._added:
            return False
        inserted = await add_web_admin(self._db, user_id)
        # An existing row means the id is an admin in the database: cache it either way.
        self._added = [*self._added, user_id]
        return inserted

    async def remove(self, user_id: int) -> bool:
        if user_id in self._owners or user_id not in self._added:
            return False
        deleted = await remove_web_admin(self._db, user_id)
        self._added = [i for i in self._added if i != user_id]
        return deleted
