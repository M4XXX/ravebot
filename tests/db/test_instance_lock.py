"""Tests for bot/db/instance_lock.py — single-instance advisory lock (mocked asyncpg).

The real-SQL counterpart is in tests/pg/test_postgres.py.
[Source: story 8-2-single-instance-guard-and-bounded-event-locks.md]
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.db import instance_lock
from bot.db.instance_lock import INSTANCE_LOCK_KEY, InstanceLock, acquire_instance_lock

DSN = "postgresql://ravebot:S3cr3tPw@db.example:5432/ravebot"
_real_sleep = asyncio.sleep  # the `fast` fixture patches asyncio.sleep


def fake_conn(locked: bool = True, pid: int = 100, holder: int | None = None) -> MagicMock:
    """A connection whose pg_try_advisory_lock answers `locked`; pg_locks shows `holder`."""
    conn = MagicMock()

    async def fetchval(query: str, *args, **kwargs):
        if "pg_try_advisory_lock" in query:
            return locked
        if "pg_locks" in query:
            return holder
        return 1  # SELECT 1 heartbeat

    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    conn.is_closed = MagicMock(return_value=False)
    conn.get_server_pid = MagicMock(return_value=pid)
    return conn


@pytest.fixture
def fast(monkeypatch):
    """Short heartbeat; retry back-off sleeps return at once."""
    monkeypatch.setattr(instance_lock, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(instance_lock.asyncio, "sleep", lambda _s: _real_sleep(0))


class TestAcquire:
    @pytest.mark.asyncio
    async def test_returns_lock_watching_its_connection(self):
        conn = fake_conn(locked=True)
        with patch("asyncpg.connect", new=AsyncMock(return_value=conn)):
            lock = await acquire_instance_lock(DSN)
        assert isinstance(lock, InstanceLock)
        conn.fetchval.assert_awaited_once_with(
            "SELECT pg_try_advisory_lock($1)", INSTANCE_LOCK_KEY
        )
        conn.add_termination_listener.assert_called_once()
        conn.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exits_when_another_instance_holds_the_lock(self):
        conn = fake_conn(locked=False)
        with patch("asyncpg.connect", new=AsyncMock(return_value=conn)):
            with pytest.raises(SystemExit) as exc_info:
                await acquire_instance_lock(DSN)
        assert "Another RaveBot instance" in str(exc_info.value)
        conn.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connection_error_is_masked(self):
        err = asyncpg.InvalidPasswordError("bad password S3cr3tPw for user ravebot")
        with patch("asyncpg.connect", new=AsyncMock(side_effect=err)):
            with pytest.raises(SystemExit) as exc_info:
                await acquire_instance_lock(DSN)
        msg = str(exc_info.value)
        assert "S3cr3tPw" not in msg
        assert "db.example:5432/ravebot" in msg

    @pytest.mark.asyncio
    async def test_query_failure_exits_masked_and_drops_connection(self):
        conn = fake_conn()
        conn.fetchval = AsyncMock(side_effect=asyncpg.PostgresError(f"boom {DSN}"))
        with patch("asyncpg.connect", new=AsyncMock(return_value=conn)):
            with pytest.raises(SystemExit) as exc_info:
                await acquire_instance_lock(DSN)
        assert "S3cr3tPw" not in str(exc_info.value)
        conn.terminate.assert_called_once()


class TestRelease:
    @pytest.mark.asyncio
    async def test_unlocks_and_closes(self):
        conn = fake_conn()
        await InstanceLock(DSN, conn).release()
        conn.execute.assert_awaited_once_with("SELECT pg_advisory_unlock($1)", INSTANCE_LOCK_KEY)
        conn.close.assert_awaited_once()
        conn.remove_termination_listener.assert_called_once()

    @pytest.mark.asyncio
    async def test_never_raises(self):
        conn = fake_conn()
        conn.execute = AsyncMock(side_effect=OSError("gone"))
        conn.close = AsyncMock(side_effect=OSError("gone"))
        await InstanceLock(DSN, conn).release()  # no exception

    @pytest.mark.asyncio
    async def test_closed_connection_skips_unlock(self):
        conn = fake_conn()
        conn.is_closed = MagicMock(return_value=True)
        await InstanceLock(DSN, conn).release()
        conn.execute.assert_not_awaited()


async def run_watch(lock: InstanceLock, on_conflict, until) -> None:
    """Run watch() until `until()` is true or it returns, then cancel it."""
    task = asyncio.create_task(lock.watch(on_conflict))
    for _ in range(500):
        if task.done() or until():
            break
        await _real_sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class TestWatch:
    @pytest.mark.asyncio
    async def test_heartbeat_keeps_running_while_connection_is_alive(self, fast):
        conn = fake_conn()
        lock = InstanceLock(DSN, conn)
        on_conflict = MagicMock()
        with patch("asyncpg.connect", new=AsyncMock()) as connect:
            await run_watch(lock, on_conflict, until=lambda: conn.fetchval.await_count >= 3)
        assert conn.fetchval.await_count >= 3  # SELECT 1 heartbeats
        connect.assert_not_called()
        on_conflict.assert_not_called()

    @pytest.mark.asyncio
    async def test_lost_connection_is_retried_until_postgres_is_back(self, fast, caplog):
        caplog.set_level("INFO")
        old, new = fake_conn(pid=100), fake_conn(pid=200)
        lock = InstanceLock(DSN, old)
        on_conflict = MagicMock()
        connect = AsyncMock(side_effect=[OSError(f"refused {DSN}"), OSError("refused"), new])
        with patch("asyncpg.connect", new=connect):
            lock._on_terminated(old)  # what asyncpg calls when the connection drops
            await run_watch(lock, on_conflict, until=lambda: "re-acquired" in caplog.text)
        assert lock._conn is new
        assert lock.conflict is False
        on_conflict.assert_not_called()
        old.terminate.assert_called_once()
        new.add_termination_listener.assert_called_once()
        assert "S3cr3tPw" not in caplog.text
        assert "re-acquired" in caplog.text

    @pytest.mark.asyncio
    async def test_failed_heartbeat_counts_as_lost(self, fast):
        old, new = fake_conn(pid=100), fake_conn(pid=200)
        old.fetchval = AsyncMock(side_effect=asyncpg.InterfaceError("connection is closed"))
        lock = InstanceLock(DSN, old)
        with patch("asyncpg.connect", new=AsyncMock(return_value=new)):
            await run_watch(lock, MagicMock(), until=lambda: lock._conn is new)
        assert lock._conn is new

    @pytest.mark.asyncio
    async def test_other_instance_holding_the_lock_is_a_conflict(self, fast):
        old = fake_conn(pid=100)
        rival = fake_conn(locked=False, holder=999)
        lock = InstanceLock(DSN, old)
        on_conflict = MagicMock()
        with patch("asyncpg.connect", new=AsyncMock(return_value=rival)):
            lock._on_terminated(old)
            await run_watch(lock, on_conflict, until=lambda: False)
        assert lock.conflict is True
        on_conflict.assert_called_once_with()
        rival.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_own_stale_session_is_terminated_not_a_conflict(self, fast):
        old = fake_conn(pid=100)
        stale = fake_conn(locked=False, holder=100)  # PostgreSQL still shows our old session
        fresh = fake_conn(pid=300)
        lock = InstanceLock(DSN, old)
        on_conflict = MagicMock()
        with patch("asyncpg.connect", new=AsyncMock(side_effect=[stale, fresh])):
            lock._on_terminated(old)
            await run_watch(lock, on_conflict, until=lambda: lock._conn is fresh)
        stale.execute.assert_awaited_once_with("SELECT pg_terminate_backend($1)", 100)
        assert lock._conn is fresh
        on_conflict.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_released_in_between_is_retried(self, fast):
        old = fake_conn(pid=100)
        racing = fake_conn(locked=False, holder=None)  # held a moment ago, free now
        fresh = fake_conn(pid=300)
        lock = InstanceLock(DSN, old)
        on_conflict = MagicMock()
        with patch("asyncpg.connect", new=AsyncMock(side_effect=[racing, fresh])):
            lock._on_terminated(old)
            await run_watch(lock, on_conflict, until=lambda: lock._conn is fresh)
        assert lock._conn is fresh
        on_conflict.assert_not_called()
