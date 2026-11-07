"""Tests for bot.services.roles (stored role ids with name fallback)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.services import roles


def _role(rid: int, name: str) -> MagicMock:
    r = MagicMock()
    r.id = rid
    r.name = name
    return r


def _guild(role_list: list) -> MagicMock:
    g = MagicMock()
    g.id = 1
    g.roles = role_list
    g.get_role = lambda rid: next((r for r in role_list if r.id == rid), None)
    return g


def _stored(value: str | None):
    return patch("bot.services.roles.get_value", AsyncMock(return_value=value))


async def test_dj_stored_id_wins_over_old_name() -> None:
    named = _role(1, "DJ")
    chosen = _role(2, "Performers")
    with _stored("2"):
        assert await roles.get_dj_role(MagicMock(), _guild([named, chosen])) is chosen


async def test_dj_missing_key_falls_back_case_insensitive() -> None:
    named = _role(1, "dj")
    with _stored(None):
        assert await roles.get_dj_role(MagicMock(), _guild([_role(3, "x"), named])) is named


async def test_dj_deleted_stored_role_returns_none_without_fallback() -> None:
    with _stored("99"):
        assert await roles.get_dj_role(MagicMock(), _guild([_role(1, "DJ")])) is None


async def test_invites_stored_id_wins() -> None:
    named = _role(1, "Active Booking")
    chosen = _role(2, "Invites")
    with _stored("2"):
        assert await roles.get_invites_role(MagicMock(), _guild([named, chosen])) is chosen


async def test_invites_fallback_is_exact_name() -> None:
    with _stored(None):
        assert await roles.get_invites_role(MagicMock(), _guild([_role(1, "active booking")])) is None
        named = _role(2, "Active Booking")
        assert await roles.get_invites_role(MagicMock(), _guild([named])) is named


async def test_invites_deleted_stored_role_returns_none() -> None:
    with _stored("99"):
        assert await roles.get_invites_role(MagicMock(), _guild([_role(1, "Active Booking")])) is None


async def test_non_numeric_stored_value_counts_as_deleted() -> None:
    with _stored("abc"):
        assert await roles.get_dj_role(MagicMock(), _guild([_role(1, "DJ")])) is None


async def test_reads_the_right_key_for_the_guild() -> None:
    m = AsyncMock(return_value=None)
    db = MagicMock()
    with patch("bot.services.roles.get_value", m):
        await roles.get_dj_role(db, _guild([]))
        await roles.get_invites_role(db, _guild([]))
    assert [c.args for c in m.await_args_list] == [(db, 1, "dj_role_id"), (db, 1, "invites_role_id")]
