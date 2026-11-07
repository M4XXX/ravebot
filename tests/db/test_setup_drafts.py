"""Tests for bot/db/setup_drafts.py and migration 0011."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_get_draft_none_when_missing() -> None:
    from bot.db.setup_drafts import get_draft

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await get_draft(pool, 1) is None


@pytest.mark.asyncio
async def test_get_draft_parses_json_string_and_dict() -> None:
    from bot.db.setup_drafts import SetupDraft, get_draft

    for raw in ('{"dj_channel": 5}', {"dj_channel": 5}):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(
            return_value={"guild_id": 1, "message_id": 2, "channel_id": 3, "step": 1, "data": raw}
        )
        assert await get_draft(pool, 1) == SetupDraft(1, 2, 3, 1, {"dj_channel": 5})


@pytest.mark.asyncio
async def test_save_draft_upserts_json() -> None:
    from bot.db.setup_drafts import save_draft

    pool = MagicMock()
    pool.execute = AsyncMock()
    await save_draft(pool, 1, 2, 3, 4, {"log_channel": 9})
    sql, *args = pool.execute.await_args.args
    assert "ON CONFLICT (guild_id) DO UPDATE" in sql and "setup_drafts" in sql
    assert args[:4] == [1, 2, 3, 4]
    assert json.loads(args[4]) == {"log_channel": 9}


@pytest.mark.asyncio
async def test_delete_draft() -> None:
    from bot.db.setup_drafts import delete_draft

    pool = MagicMock()
    pool.execute = AsyncMock()
    await delete_draft(pool, 7)
    sql, guild_id = pool.execute.await_args.args
    assert "DELETE FROM setup_drafts" in sql and guild_id == 7


def test_migration_0011_revises_0010() -> None:
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0011_setup_drafts.py"
    spec = importlib.util.spec_from_file_location("m0011", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0011", "0010")
