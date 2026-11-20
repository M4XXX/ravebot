"""Unit checks of the rebuild from Notion and the startup cases (bot/sync/rebuild.py).

The real-SQL checks (data replaced, admins kept, rollback) are in tests/pg/test_rebuild.py.
"""
import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.logo import LogoError
from bot.sync import rebuild
from bot.sync.notion_import import ImportResult
from bot.ui import texts


class _FakeCycle:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.resync_all = AsyncMock(return_value=5)

    @contextlib.asynccontextmanager
    async def exclusive(self):  # noqa: ANN202
        async with self.lock:
            yield


def _pool(*, pg_has_data: bool = False, imported: str | None = "1") -> MagicMock:
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=pg_has_data)
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock()
    return pool


def _ctx(*, notion: bool = True, cycle: bool = True, pool: MagicMock | None = None):  # noqa: ANN202
    client = MagicMock() if notion else None
    if client is not None:
        client.has_rows = AsyncMock(return_value=True)
    return SimpleNamespace(
        db=pool or _pool(),
        notion_client=client,
        maintenance=None,
        sync_cycle=_FakeCycle() if cycle else None,
        mirror=None,
        log_service=SimpleNamespace(log_notice=AsyncMock(), log_attention=AsyncMock()),
        rebuild_report=None,
        sync_problem=None,
    )


@pytest.fixture
def patched(monkeypatch):  # noqa: ANN001, ANN201
    """Safety export, import and config stubbed; returns the mocks."""
    mocks = SimpleNamespace(
        export=AsyncMock(return_value="/backups/x.json.gz"),
        imp=AsyncMock(return_value=ImportResult(True, 2, 5, 3, [], ["a note"])),
        get_value=AsyncMock(return_value="1"),
        set_value=AsyncMock(),
        download=AsyncMock(return_value=b"png"),
    )
    monkeypatch.setattr(rebuild, "write_safety_export", mocks.export)
    monkeypatch.setattr(rebuild, "import_from_notion", mocks.imp)
    monkeypatch.setattr(rebuild, "get_value", mocks.get_value)
    monkeypatch.setattr(rebuild, "set_value", mocks.set_value)
    monkeypatch.setattr(rebuild, "download_logo_file", mocks.download)
    return mocks


# ---- rebuild_from_notion ----


async def test_rebuild_refuses_without_notion(patched) -> None:  # noqa: ANN001
    ctx = _ctx(notion=False)
    result = await rebuild.rebuild_from_notion(ctx)
    assert not result.ok and result.message == texts.REBUILD_NO_NOTION
    patched.export.assert_not_awaited()
    patched.imp.assert_not_awaited()


async def test_second_operation_is_refused_while_one_runs(patched) -> None:  # noqa: ANN001
    from bot import maintenance

    ctx = _ctx()
    claim = maintenance.try_claim("import")
    assert claim is not None
    with claim:
        result = await rebuild.rebuild_from_notion(ctx)
    assert not result.ok and result.message == texts.OPERATION_BUSY
    patched.export.assert_not_awaited()
    assert ctx.maintenance is None


async def test_rebuild_holds_the_claim_while_it_runs(patched) -> None:  # noqa: ANN001
    from bot import maintenance

    seen: list = []

    async def imp(pool, client, *, mode):  # noqa: ANN001, ANN202
        seen.append(maintenance.running())
        second = await rebuild.rebuild_from_notion(_ctx())
        seen.append(second.message)
        return ImportResult(True, 0, 0, 0, [])

    patched.imp.side_effect = imp
    assert (await rebuild.rebuild_from_notion(_ctx())).ok
    assert seen == ["rebuild", texts.OPERATION_BUSY]
    assert maintenance.running() is None


async def test_rebuild_is_allowed_after_a_failed_upgrade_import(patched) -> None:  # noqa: ANN001
    ctx = _ctx()
    ctx.maintenance, ctx.sync_problem = texts.MAINTENANCE, "Two events titled 'x'"
    result = await rebuild.rebuild_from_notion(ctx)
    assert result.ok, result.message
    assert ctx.maintenance is None and ctx.sync_problem is None


async def test_failed_rebuild_keeps_the_failed_upgrade_pause(patched) -> None:  # noqa: ANN001
    patched.imp.return_value = ImportResult(False, 0, 0, 0, ["bad"])
    ctx = _ctx()
    ctx.maintenance, ctx.sync_problem = texts.MAINTENANCE, "old problem"
    result = await rebuild.rebuild_from_notion(ctx)
    assert not result.ok
    assert ctx.maintenance == texts.MAINTENANCE and ctx.sync_problem == "old problem"


async def test_rebuild_stops_when_the_safety_export_fails(patched) -> None:  # noqa: ANN001
    patched.export.side_effect = OSError("disk full")
    ctx = _ctx()
    result = await rebuild.rebuild_from_notion(ctx)
    assert not result.ok and result.message == texts.REBUILD_SAFETY_FAILED
    patched.imp.assert_not_awaited()
    assert ctx.maintenance is None


async def test_rebuild_runs_export_then_replace_import_in_maintenance(patched) -> None:  # noqa: ANN001
    ctx = _ctx()
    seen: list = []

    async def imp(pool, client, *, mode):  # noqa: ANN001, ANN202
        seen.append((mode, ctx.maintenance, ctx.sync_cycle.lock.locked()))
        return ImportResult(True, 2, 5, 3, [], [])

    patched.imp.side_effect = imp
    result = await rebuild.rebuild_from_notion(ctx)
    assert result.ok, result.message
    patched.export.assert_awaited_once_with(ctx.db, "before-rebuild")
    assert seen == [("replace", texts.MAINTENANCE, True)]
    assert ctx.maintenance is None
    patched.set_value.assert_awaited_once_with(
        ctx.db, 0, rebuild.PG_TRUTH_IMPORTED_KEY, "1"
    )
    ctx.log_service.log_notice.assert_awaited_once()
    assert ctx.log_service.log_notice.await_args.args[0] == texts.LOG_REBUILD_DONE_TITLE


async def test_rebuild_failure_posts_problems_and_changes_nothing_else(patched) -> None:  # noqa: ANN001
    patched.imp.return_value = ImportResult(False, 0, 0, 0, ["Two events titled '*x*'"])
    ctx = _ctx()
    result = await rebuild.rebuild_from_notion(ctx)
    assert not result.ok and result.message == texts.REBUILD_FAILED
    patched.set_value.assert_not_awaited()
    patched.download.assert_not_awaited()
    assert ctx.maintenance is None
    title, what, _todo = ctx.log_service.log_attention.await_args.args
    assert title == texts.LOG_REBUILD_FAILED_TITLE
    assert "\\*x\\*" in what  # Notion text is escaped


async def test_rebuild_import_exception_is_a_failure(patched) -> None:  # noqa: ANN001
    patched.imp.side_effect = RuntimeError("boom")
    ctx = _ctx()
    result = await rebuild.rebuild_from_notion(ctx)
    assert not result.ok and result.message == texts.REBUILD_FAILED
    assert ctx.maintenance is None


async def test_rebuild_without_cycle_still_runs(patched) -> None:  # noqa: ANN001
    ctx = _ctx(cycle=False)
    assert (await rebuild.rebuild_from_notion(ctx)).ok


async def test_rebuild_waits_for_a_running_cycle(patched) -> None:  # noqa: ANN001
    ctx = _ctx()
    await ctx.sync_cycle.lock.acquire()
    task = asyncio.create_task(rebuild.rebuild_from_notion(ctx))
    await asyncio.sleep(0.05)
    assert not task.done() and ctx.maintenance == texts.MAINTENANCE
    patched.imp.assert_not_awaited()
    ctx.sync_cycle.lock.release()
    assert (await task).ok
    assert ctx.maintenance is None


async def test_rebuild_downloads_logos_and_lists_failures(patched) -> None:  # noqa: ANN001
    patched.imp.return_value = ImportResult(
        True, 0, 0, 3, [], [],
        logos={
            1: ("file", "https://files.example/a.png"),
            2: ("file", "https://files.example/b.png"),
            3: ("external", "https://example.org/c.png"),
        },
    )

    async def download(url, *, client=None):  # noqa: ANN001, ANN202
        if url.endswith("b.png"):
            raise LogoError("download_failed")
        return b"png-a"

    patched.download.side_effect = download
    pool = _pool()
    pool.fetch = AsyncMock(return_value=[
        {"discord_user_id": 1, "dj_name": "A"}, {"discord_user_id": 2, "dj_name": "*B*"},
        {"discord_user_id": 3, "dj_name": "C"},
    ])
    ctx = _ctx(pool=pool)
    result = await rebuild.rebuild_from_notion(ctx)
    assert result.ok
    assert "1" in result.message  # one logo could not be downloaded
    writes = [c.args for c in pool.execute.await_args_list]
    assert any(1 in w and b"png-a" in w for w in writes)
    assert any(3 in w and "https://example.org/c.png" in w for w in writes)
    assert not any(2 in w[1:] for w in writes)
    sql = " ".join(w[0] for w in writes)
    assert "logo_synced = true" in sql
    what = ctx.log_service.log_notice.await_args.args[1]
    assert "\\*B\\*" in what


async def test_rebuild_report_waits_when_not_posted(patched) -> None:  # noqa: ANN001
    ctx = _ctx()
    await rebuild.rebuild_from_notion(ctx, post_result=False)
    ctx.log_service.log_notice.assert_not_awaited()
    assert ctx.rebuild_report is not None
    await rebuild.post_rebuild_result(ctx)
    ctx.log_service.log_notice.assert_awaited_once()
    assert ctx.rebuild_report is None
    await rebuild.post_rebuild_result(ctx)  # posted once
    ctx.log_service.log_notice.assert_awaited_once()


# ---- startup: rebuild instead of the upgrade import (R19) ----


async def test_startup_rebuild_replaces_the_upgrade_import(patched, monkeypatch) -> None:  # noqa: ANN001
    patched.get_value.return_value = None  # upgrade import never ran (or Postgres was lost)
    ctx = _ctx(pool=_pool(pg_has_data=False))
    rebuild_mock = AsyncMock(return_value=rebuild.ActionResult(True, "ok"))
    monkeypatch.setattr(rebuild, "rebuild_from_notion", rebuild_mock)
    assert await rebuild.run_startup_rebuild_if_needed(ctx) is True
    rebuild_mock.assert_awaited_once_with(ctx, post_result=False)
    assert ctx.maintenance is None


async def test_failed_startup_rebuild_pauses_like_a_failed_upgrade(patched, monkeypatch) -> None:  # noqa: ANN001
    patched.get_value.return_value = None
    ctx = _ctx(pool=_pool(pg_has_data=False))
    ctx.sync_problem = None
    patched.imp.return_value = ImportResult(False, 0, 0, 0, ["Two events titled 'x'"])
    assert await rebuild.run_startup_rebuild_if_needed(ctx) is True
    assert ctx.maintenance == texts.MAINTENANCE
    assert "Two events" in ctx.sync_problem
    patched.set_value.assert_not_awaited()  # the next start retries


async def test_startup_rebuild_not_needed(patched, monkeypatch) -> None:  # noqa: ANN001
    rebuild_mock = AsyncMock()
    monkeypatch.setattr(rebuild, "rebuild_from_notion", rebuild_mock)
    assert await rebuild.run_startup_rebuild_if_needed(_ctx(notion=False)) is False
    assert await rebuild.run_startup_rebuild_if_needed(_ctx()) is False  # upgrade done
    patched.get_value.return_value = None
    filled = _ctx(pool=_pool(pg_has_data=True))  # an upgrade: Postgres has DJs
    assert await rebuild.run_startup_rebuild_if_needed(filled) is False
    empty_notion = _ctx(pool=_pool(pg_has_data=False))
    empty_notion.notion_client.has_rows = AsyncMock(return_value=False)
    assert await rebuild.run_startup_rebuild_if_needed(empty_notion) is False
    broken = _ctx(pool=_pool(pg_has_data=False))
    broken.notion_client.has_rows = AsyncMock(side_effect=RuntimeError("down"))
    assert await rebuild.run_startup_rebuild_if_needed(broken) is False
    rebuild_mock.assert_not_awaited()


# ---- startup cases ----


async def test_startup_empty_pg_rebuilds(patched, monkeypatch) -> None:  # noqa: ANN001
    ctx = _ctx(pool=_pool(pg_has_data=False))
    rebuild_mock = AsyncMock()
    monkeypatch.setattr(rebuild, "rebuild_from_notion", rebuild_mock)
    await rebuild.run_startup_recovery(ctx)
    rebuild_mock.assert_awaited_once_with(ctx, post_result=False)
    ctx.notion_client.has_rows.assert_awaited_with("events")
    ctx.sync_cycle.resync_all.assert_not_awaited()


async def test_startup_empty_pg_and_empty_notion_does_nothing(patched, monkeypatch) -> None:  # noqa: ANN001
    ctx = _ctx(pool=_pool(pg_has_data=False))
    ctx.notion_client.has_rows = AsyncMock(return_value=False)
    rebuild_mock = AsyncMock()
    monkeypatch.setattr(rebuild, "rebuild_from_notion", rebuild_mock)
    await rebuild.run_startup_recovery(ctx)
    rebuild_mock.assert_not_awaited()
    ctx.sync_cycle.resync_all.assert_not_awaited()


async def test_startup_empty_notion_pushes_all(patched, monkeypatch) -> None:  # noqa: ANN001
    pool = _pool(pg_has_data=True)
    ctx = _ctx(pool=pool)
    ctx.notion_client.has_rows = AsyncMock(return_value=False)
    rebuild_mock = AsyncMock()
    monkeypatch.setattr(rebuild, "rebuild_from_notion", rebuild_mock)
    seen: list = []

    async def resync():  # noqa: ANN202
        seen.append(ctx.sync_cycle.lock.locked())
        return 5

    ctx.sync_cycle.resync_all.side_effect = resync
    await rebuild.run_startup_recovery(ctx)
    rebuild_mock.assert_not_awaited()
    assert seen == [True]  # under the cycle lock
    # Mirror rows point into databases that no longer hold the pages: forgotten first.
    assert "DELETE FROM notion_mirror" in pool.execute.await_args_list[0].args[0]


async def test_startup_filled_pg_and_filled_notion_does_nothing(patched, monkeypatch) -> None:  # noqa: ANN001
    pool = _pool(pg_has_data=True)
    ctx = _ctx(pool=pool)
    rebuild_mock = AsyncMock()
    monkeypatch.setattr(rebuild, "rebuild_from_notion", rebuild_mock)
    await rebuild.run_startup_recovery(ctx)
    rebuild_mock.assert_not_awaited()
    ctx.sync_cycle.resync_all.assert_not_awaited()
    pool.execute.assert_not_awaited()


async def test_startup_cases_need_notion_and_the_finished_upgrade(patched, monkeypatch) -> None:  # noqa: ANN001
    rebuild_mock = AsyncMock()
    monkeypatch.setattr(rebuild, "rebuild_from_notion", rebuild_mock)
    plain = _ctx(notion=False)
    await rebuild.run_startup_recovery(plain)
    plain.db.fetchval.assert_not_awaited()
    paused = _ctx()
    paused.maintenance = "restoring"  # the upgrade import failed
    await rebuild.run_startup_recovery(paused)
    paused.db.fetchval.assert_not_awaited()
    patched.get_value.return_value = None  # the upgrade import never ran
    pending = _ctx()
    await rebuild.run_startup_recovery(pending)
    pending.db.fetchval.assert_not_awaited()
    rebuild_mock.assert_not_awaited()


async def test_startup_case_failure_is_logged_not_raised(patched, monkeypatch) -> None:  # noqa: ANN001
    ctx = _ctx(pool=_pool(pg_has_data=True))
    ctx.notion_client.has_rows = AsyncMock(side_effect=RuntimeError("notion down"))
    await rebuild.run_startup_recovery(ctx)  # does not raise
    ctx.sync_cycle.resync_all.assert_not_awaited()
    assert ctx.maintenance is None
