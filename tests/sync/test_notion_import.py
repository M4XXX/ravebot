"""Tests for bot/sync/notion_import.py without a database.

The import's SQL is covered in tests/pg/test_notion_import.py; here the upgrade
gate (config key, safety export, maintenance) and the validation before any write.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.sync import notion_import
from bot.sync.notion_import import (
    PG_TRUTH_IMPORTED_KEY,
    ImportResult,
    import_from_notion,
    post_upgrade_result,
    run_upgrade_import_if_needed,
)
from bot.ui import texts
from tests.conftest import make_real_ctx
from tests.sync.pages import dj_page, event_page, slot_page, snapshot


def _ctx(stored: str | None = None) -> tuple[MagicMock, dict]:
    ctx = make_real_ctx(db=MagicMock())
    ctx.notion_client = MagicMock()
    ctx.notion_client.read_all = AsyncMock(return_value=snapshot())
    ctx.log_service = MagicMock()
    ctx.log_service.log_attention = AsyncMock()
    ctx.log_service.log_notice = AsyncMock()
    config = {PG_TRUTH_IMPORTED_KEY: stored} if stored is not None else {}
    return ctx, config


class _Patches:
    """Patch config access, the safety export and the import; record the call order."""

    def __init__(self, config: dict, result: ImportResult | None = None) -> None:
        self.config = config
        self.calls: list[str] = []
        self.result = result or ImportResult(ok=True, events=2, slots=5, djs=3, problems=[])
        self.maintenance_during_import: str | None = None

    async def get_value(self, db, guild_id, key):  # noqa: ANN001, ANN202
        assert guild_id == 0
        return self.config.get(key)

    async def set_value(self, db, guild_id, key, value):  # noqa: ANN001, ANN202
        assert guild_id == 0
        self.calls.append(f"set {key}={value}")
        self.config[key] = value

    async def export(self, pool, reason):  # noqa: ANN001, ANN202
        self.calls.append(f"export {reason}")
        return Path("/tmp/x.ravebot.json.gz")  # noqa: S108

    def importer(self, ctx):  # noqa: ANN001, ANN202
        async def run(pool, client, *, mode):  # noqa: ANN001, ANN202
            self.calls.append(f"import {mode}")
            self.maintenance_during_import = ctx.maintenance
            return self.result

        return run

    def apply(self, ctx):  # noqa: ANN001, ANN202
        return (
            patch.object(notion_import, "get_value", new=self.get_value),
            patch.object(notion_import, "set_value", new=self.set_value),
            patch.object(notion_import, "write_safety_export", new=self.export),
            patch.object(notion_import, "import_from_notion", new=self.importer(ctx)),
        )


async def _run(ctx, patches: _Patches) -> None:  # noqa: ANN001
    a, b, c, d = patches.apply(ctx)
    with a, b, c, d:
        await run_upgrade_import_if_needed(ctx)


async def test_upgrade_runs_once() -> None:
    ctx, config = _ctx(stored="1")
    p = _Patches(config)
    await _run(ctx, p)
    assert p.calls == []
    ctx.notion_client.read_all.assert_not_awaited()
    assert ctx.maintenance is None
    assert ctx.upgrade_import is None


async def test_upgrade_skipped_without_notion() -> None:
    ctx, config = _ctx()
    ctx.notion_client = None
    p = _Patches(config)
    await _run(ctx, p)
    assert p.calls == []
    assert ctx.maintenance is None


async def test_upgrade_writes_safety_export_first() -> None:
    ctx, config = _ctx()
    p = _Patches(config)
    await _run(ctx, p)
    assert p.calls == [
        "export upgrade", "import upgrade", f"set {PG_TRUTH_IMPORTED_KEY}=1",
    ]
    assert p.maintenance_during_import == texts.MAINTENANCE
    assert ctx.maintenance is None
    assert ctx.sync_problem is None
    assert ctx.upgrade_import == p.result


async def test_failed_upgrade_stays_in_maintenance() -> None:
    ctx, config = _ctx()
    problems = [f"problem {n}" for n in range(12)]
    p = _Patches(config, ImportResult(ok=False, events=0, slots=0, djs=0, problems=problems))
    await _run(ctx, p)
    assert p.calls == ["export upgrade", "import upgrade"]
    assert PG_TRUTH_IMPORTED_KEY not in config
    assert ctx.maintenance == texts.MAINTENANCE
    assert ctx.sync_problem == "; ".join(problems[:10])
    assert ctx.upgrade_import.ok is False


async def test_failed_safety_export_imports_nothing() -> None:
    ctx, config = _ctx()
    p = _Patches(config)

    async def broken(pool, reason):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    p.export = broken
    await _run(ctx, p)
    assert p.calls == []
    assert ctx.maintenance == texts.MAINTENANCE
    assert "disk full" in ctx.sync_problem


async def test_post_upgrade_result_posts_once() -> None:
    ctx, _ = _ctx()
    ctx.upgrade_import = ImportResult(ok=True, events=2, slots=5, djs=3, problems=[])
    await post_upgrade_result(ctx)
    await post_upgrade_result(ctx)
    ctx.log_service.log_notice.assert_awaited_once()
    title, text = ctx.log_service.log_notice.await_args.args
    assert title == texts.LOG_UPGRADE_DONE_TITLE
    assert "2 events, 5 slots and 3 DJs" in text
    assert ctx.upgrade_import is None


async def test_post_failed_upgrade_lists_problems_escaped() -> None:
    ctx, _ = _ctx()
    ctx.upgrade_import = ImportResult(
        ok=False, events=0, slots=0, djs=0, problems=["Two events are titled '*Rave*'"]
    )
    await post_upgrade_result(ctx)
    title, text, todo = ctx.log_service.log_attention.await_args.args
    assert title == texts.LOG_UPGRADE_FAILED_TITLE
    assert "\\*Rave\\*" in text
    assert todo == texts.LOG_UPGRADE_FAILED_TODO


def _client(snap) -> MagicMock:  # noqa: ANN001
    client = MagicMock()
    client.read_all = AsyncMock(return_value=snap)
    return client


async def test_duplicate_titles_fail_before_any_write() -> None:
    pool = MagicMock()
    snap = snapshot(events=[event_page("p1", "Rave"), event_page("p2", "Rave")])
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok is False
    assert any("Rave" in p for p in result.problems)
    pool.acquire.assert_not_called()


async def test_duplicate_discord_ids_fail_before_any_write() -> None:
    pool = MagicMock()
    snap = snapshot(djs=[dj_page("d1", "A", 5), dj_page("d2", "B", 5)])
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok is False
    assert any("5" in p for p in result.problems)
    pool.acquire.assert_not_called()


@pytest.mark.parametrize(
    "slot",
    [
        slot_page("s1", "missing-event", 1, "d1"),  # booked slot, unknown event
        slot_page("s1", None, 1, "d1"),  # booked slot without an event
        slot_page("s1", "p1", 4, "d1"),  # booked slot above slot_count (3)
    ],
)
async def test_booked_orphan_slot_fails_before_any_write(slot) -> None:  # noqa: ANN001
    pool = MagicMock()
    snap = snapshot(
        events=[event_page("p1", "Rave")], slots=[slot], djs=[dj_page("d1", "A", 5)]
    )
    result = await import_from_notion(pool, _client(snap), mode="upgrade")
    assert result.ok is False and result.problems
    pool.acquire.assert_not_called()


async def test_unreadable_notion_fails() -> None:
    client = MagicMock()
    client.read_all = AsyncMock(side_effect=RuntimeError("Notion is down"))
    result = await import_from_notion(MagicMock(), client, mode="upgrade")
    assert result.ok is False
    assert "Notion is down" in result.problems[0]
