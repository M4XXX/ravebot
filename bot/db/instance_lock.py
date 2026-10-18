"""Single-instance guard — a PostgreSQL advisory lock held for the bot's lifetime.

Taken in bot/main.py before migrations. A second bot against the same database finds the
lock held and exits, so the per-event Notion write lock in NotionClient (which only works
inside one process) cannot be bypassed.

The lock is session-level and lives on a dedicated connection (never a pool connection,
which would be recycled): PostgreSQL releases it when the process exits or the connection
is lost, and a new instance can start.

While running, InstanceLock.watch() checks the connection every HEARTBEAT_SECONDS (which
also keeps idle-timeout firewalls from dropping it). When the connection is lost the bot
keeps running and takes the lock again as soon as PostgreSQL is back (NFR9: no manual
restart after a database outage). Only if another instance got the lock meanwhile does it
report a conflict, and bot/main.py shuts down with a non-zero exit code.

[Source: epics.md#Story 8.2]
[Source: prd.md#NFR9]
[Source: story 8-2-single-instance-guard-and-bounded-event-locks.md]
"""
import asyncio
import logging
import sys
from collections.abc import Callable
from typing import NoReturn

import asyncpg

from bot import redact
from bot.db.pool import _safe_target

logger = logging.getLogger(__name__)

# Fixed key for pg_try_advisory_lock (first 8 bytes of sha256("ravebot-single-instance")).
# Never change it: a changed key lets an old and a new version run side by side.
INSTANCE_LOCK_KEY = 5846470656324291975

ALREADY_RUNNING = (
    "Another RaveBot instance is already running against this database — refusing to start. "
    "Stop the other instance (or wait for it to exit) and start again."
)
LOST_TO_OTHER = (
    "Another RaveBot instance took the single-instance lock while the database connection "
    "was lost — shutting down."
)

HEARTBEAT_SECONDS = 30.0
HEARTBEAT_TIMEOUT_SECONDS = 10.0
RETRY_MAX_SECONDS = 60.0

# pg_locks shows a bigint advisory key as two oids plus objsubid = 1
_HOLDER_QUERY = (
    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
    " AND classid::bigint = $1 AND objid::bigint = $2 AND objsubid = 1"
)
_KEY_HIGH = (INSTANCE_LOCK_KEY >> 32) & 0xFFFFFFFF
_KEY_LOW = INSTANCE_LOCK_KEY & 0xFFFFFFFF


class InstanceLock:
    """The held lock and its connection; see acquire_instance_lock()."""

    def __init__(self, postgres_url: str, conn: asyncpg.Connection) -> None:
        self._url = postgres_url
        self._conn = conn
        self._lost = asyncio.Event()
        self.conflict = False  # another instance took the lock: shut down, exit non-zero
        self._watch_connection()

    def _watch_connection(self) -> None:
        self._pid = self._conn.get_server_pid()  # unavailable once the connection is gone
        self._lost.clear()
        self._conn.add_termination_listener(self._on_terminated)

    def _on_terminated(self, _conn: object) -> None:
        self._lost.set()

    async def watch(self, on_conflict: Callable[[], None]) -> None:
        """Run until cancelled: heartbeat, and take the lock again after a lost connection.

        Calls on_conflict() and returns if another instance holds the lock by then.
        """
        while True:
            try:
                await asyncio.wait_for(self._lost.wait(), HEARTBEAT_SECONDS)
            except TimeoutError:
                if await self._alive():
                    continue
            logger.error(
                "Lost the database connection holding the single-instance lock — "
                "retrying until PostgreSQL is back"
            )
            if not await self._regain():
                self.conflict = True
                logger.error(LOST_TO_OTHER)
                on_conflict()
                return
            logger.info("Single-instance lock re-acquired.")

    async def _alive(self) -> bool:
        try:
            await self._conn.fetchval("SELECT 1", timeout=HEARTBEAT_TIMEOUT_SECONDS)
            return True
        except Exception:  # noqa: BLE001 - any failure means the session is gone for us
            return False

    async def _regain(self) -> bool:
        """Reconnect and take the lock; False if another instance holds it. Never gives up
        while PostgreSQL is unreachable — scheduled work is paused by the outage anyway."""
        old_pid = self._pid
        self._conn.remove_termination_listener(self._on_terminated)
        self._conn.terminate()
        delay = 1.0
        while True:
            try:
                conn = await asyncpg.connect(self._url)
            except (OSError, ValueError, asyncpg.PostgresError, TimeoutError) as exc:
                logger.warning(
                    "Single-instance lock: PostgreSQL not reachable (%s), retrying in %.0f s",
                    redact.redact(str(exc), redact.secret_values(self._url)), delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                continue
            try:
                if await conn.fetchval("SELECT pg_try_advisory_lock($1)", INSTANCE_LOCK_KEY):
                    self._conn = conn
                    self._watch_connection()
                    return True
                holder = await conn.fetchval(_HOLDER_QUERY, _KEY_HIGH, _KEY_LOW)
                if holder is not None and holder != old_pid:
                    await conn.close()
                    return False
                if holder == old_pid:
                    # Our own old session, which PostgreSQL has not dropped yet
                    await conn.execute("SELECT pg_terminate_backend($1)", old_pid)
                await conn.close()  # and try again (holder None: released just now)
                await asyncio.sleep(1.0)
                continue
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
                conn.terminate()
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_SECONDS)

    async def release(self) -> None:
        """Release the lock and close its connection. Never raises (runs during shutdown)."""
        conn = self._conn
        conn.remove_termination_listener(self._on_terminated)
        try:
            if not conn.is_closed():
                await conn.execute("SELECT pg_advisory_unlock($1)", INSTANCE_LOCK_KEY)
        except Exception:
            logger.warning("Could not release the single-instance lock", exc_info=True)
        try:
            await conn.close()
        except Exception:
            logger.warning("Could not close the single-instance lock connection", exc_info=True)


async def acquire_instance_lock(postgres_url: str) -> InstanceLock:
    """Open a dedicated connection and take the instance lock on it, or exit."""

    def fail(exc: BaseException) -> NoReturn:
        sys.exit(
            f"Failed to connect to PostgreSQL — cannot start bot.\n"
            f"Check POSTGRES_URL (host {_safe_target(postgres_url)})\n"
            f"Error: {redact.redact(str(exc), redact.secret_values(postgres_url))}"
        )

    try:
        conn = await asyncpg.connect(postgres_url)
    except (OSError, ValueError, asyncpg.PostgresError, TimeoutError) as exc:
        fail(exc)
    try:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", INSTANCE_LOCK_KEY)
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        conn.terminate()
        fail(exc)
    if not locked:
        await conn.close()
        sys.exit(ALREADY_RUNNING)
    logger.info("Single-instance lock acquired.")
    return InstanceLock(postgres_url, conn)
