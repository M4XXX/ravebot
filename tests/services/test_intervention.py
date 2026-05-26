"""Unit tests for bot/services/intervention.py -- Story 6.2.

Tests cover: InterventionService.start, send_message, end.
All Discord API calls are mocked.

[Source: story 6-2-admin-intervention-take-over-dj-dm.md#Task 11]
"""
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest


def _make_service(user=None):
    """Build an InterventionService with mock bot, db, and log_service."""
    from bot.services.intervention import InterventionService

    bot = MagicMock()
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)

    db = MagicMock()
    log_service = MagicMock()
    log_service.post_intervention = AsyncMock()

    return InterventionService(bot=bot, db=db, log_service=log_service)


@pytest.mark.asyncio
async def test_start_calls_db_and_logs_to_admin(monkeypatch):
    """start() inserts intervention row and posts log entry."""
    svc = _make_service()

    start_mock = AsyncMock()
    monkeypatch.setattr("bot.services.intervention.start_intervention", start_mock)

    await svc.start(discord_user_id=42, dj_name="DJ Scope")

    start_mock.assert_awaited_once_with(svc.db, 42)
    svc.log_service.post_intervention.assert_awaited_once()
    call = svc.log_service.post_intervention.call_args
    assert call.kwargs["sender"] == "ADMIN"
    assert "@DJ Scope" in call.kwargs["recipient"]
    assert "paused" in call.kwargs["content"].lower()


@pytest.mark.asyncio
async def test_send_message_sends_dm_and_logs(monkeypatch):
    """send_message() sends DM to DJ, logs [ADMIN -> @DJName], and returns True."""
    user = MagicMock()
    user.send = AsyncMock()
    svc = _make_service(user=user)

    result = await svc.send_message(discord_user_id=7, dj_name="DJ Beam", message_text="Hey there!")

    assert result is True
    user.send.assert_awaited_once_with("Hey there!")
    svc.log_service.post_intervention.assert_awaited_once()
    call = svc.log_service.post_intervention.call_args
    assert call.kwargs["sender"] == "ADMIN"
    assert "@DJ Beam" in call.kwargs["recipient"]
    assert call.kwargs["content"] == "Hey there!"


@pytest.mark.asyncio
async def test_send_message_forbidden_does_not_raise():
    """send_message() catches Forbidden, does not raise, and returns False."""
    user = MagicMock()
    user.send = AsyncMock(side_effect=disnake.Forbidden(MagicMock(), "DMs blocked"))
    svc = _make_service(user=user)

    result = await svc.send_message(discord_user_id=8, dj_name="DJ Lock", message_text="test")

    assert result is False
    user.send.assert_awaited_once()
    # log_service should NOT have been called (send failed before logging)
    svc.log_service.post_intervention.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_user_not_found_does_not_raise():
    """send_message() handles user not found gracefully and returns False."""
    from bot.services.intervention import InterventionService

    bot = MagicMock()
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(side_effect=disnake.NotFound(MagicMock(), "Unknown user"))

    svc = InterventionService(bot=bot, db=MagicMock(), log_service=MagicMock())
    svc.log_service.post_intervention = AsyncMock()

    result = await svc.send_message(discord_user_id=0, dj_name="Ghost DJ", message_text="hello")

    assert result is False
    svc.log_service.post_intervention.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_fetches_user_when_not_in_cache():
    """send_message() falls back to fetch_user when get_user returns None, returns True."""
    from bot.services.intervention import InterventionService

    fetched_user = MagicMock()
    fetched_user.send = AsyncMock()

    bot = MagicMock()
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=fetched_user)

    log_service = MagicMock()
    log_service.post_intervention = AsyncMock()

    svc = InterventionService(bot=bot, db=MagicMock(), log_service=log_service)
    result = await svc.send_message(discord_user_id=99, dj_name="DJ Fetch", message_text="hi")

    assert result is True
    bot.fetch_user.assert_awaited_once_with(99)
    fetched_user.send.assert_awaited_once_with("hi")


@pytest.mark.asyncio
async def test_end_calls_db_and_logs_to_admin(monkeypatch):
    """end() removes intervention row and posts log entry."""
    svc = _make_service()

    end_mock = AsyncMock()
    monkeypatch.setattr("bot.services.intervention.end_intervention", end_mock)

    await svc.end(discord_user_id=55, dj_name="DJ Exit")

    end_mock.assert_awaited_once_with(svc.db, 55)
    svc.log_service.post_intervention.assert_awaited_once()
    call = svc.log_service.post_intervention.call_args
    assert call.kwargs["sender"] == "BOT"
    assert "DJ Exit" in call.kwargs["content"]
    assert "resumed" in call.kwargs["content"].lower()
