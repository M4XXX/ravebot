"""Tests for bot/cogs/outreach_cog.py — loop start-up behaviour."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_outreach_loop_waits_until_ready_before_first_iteration() -> None:
    from bot.cogs.outreach_cog import OutreachCog

    loop = OutreachCog._outreach_check
    assert loop._before_loop is not None

    cog = OutreachCog(MagicMock())
    cog.bot = MagicMock()
    cog.bot.wait_until_ready = AsyncMock()
    await loop._before_loop(cog)

    cog.bot.wait_until_ready.assert_awaited_once()


def _make_cog(enabled_value: str | None) -> tuple["object", MagicMock, MagicMock]:
    from bot.cogs.outreach_cog import OutreachCog

    ctx = MagicMock()
    ctx.db = MagicMock()
    ctx.db.fetchval = AsyncMock(return_value=1)
    ctx.notion_client = MagicMock()
    svc = MagicMock()
    svc.run_gap_detection = AsyncMock(return_value=[MagicMock()])
    svc.contact_next_dj = AsyncMock(return_value=False)
    svc.handle_exhaustion = AsyncMock()
    ctx.outreach_service = svc
    cog = OutreachCog(ctx)
    cog.bot = MagicMock()
    cog.bot.guilds = [MagicMock(id=111)]
    return cog, svc, ctx


@pytest.mark.asyncio
async def test_outreach_check_does_nothing_when_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog, svc, _ = _make_cog("false")
    monkeypatch.setattr("bot.services.outreach.get_value", AsyncMock(return_value="false"))
    await cog._outreach_check.coro(cog)
    svc.run_gap_detection.assert_not_called()
    svc.contact_next_dj.assert_not_called()
    svc.handle_exhaustion.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "true"])
async def test_outreach_check_runs_when_switch_on_or_unset(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    cog, svc, _ = _make_cog(value)
    monkeypatch.setattr("bot.services.outreach.get_value", AsyncMock(return_value=value))
    await cog._outreach_check.coro(cog)
    svc.run_gap_detection.assert_awaited_once()
    svc.contact_next_dj.assert_awaited_once()
    svc.handle_exhaustion.assert_awaited_once()


@pytest.mark.asyncio
async def test_outreach_check_reads_switch_fresh_each_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog, svc, _ = _make_cog(None)
    getter = AsyncMock(side_effect=["false", "true"])
    monkeypatch.setattr("bot.services.outreach.get_value", getter)
    await cog._outreach_check.coro(cog)
    svc.run_gap_detection.assert_not_called()
    await cog._outreach_check.coro(cog)
    svc.run_gap_detection.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [(None, True), ("true", True), ("false", False)])
async def test_outreach_enabled_helper(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    from bot.services.outreach import outreach_enabled

    getter = AsyncMock(return_value=raw)
    monkeypatch.setattr("bot.services.outreach.get_value", getter)
    assert await outreach_enabled(MagicMock(), 111) is expected
    assert getter.await_args.args[2] == "outreach_enabled"


@pytest.mark.asyncio
async def test_outreach_check_switch_read_failure_skips_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog, svc, _ = _make_cog(None)
    monkeypatch.setattr(
        "bot.services.outreach.get_value", AsyncMock(side_effect=RuntimeError("db"))
    )
    await cog._outreach_check.coro(cog)
    svc.run_gap_detection.assert_not_called()
