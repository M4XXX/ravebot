"""Tests for bot/services/lineup.py — LineupService.post_update()."""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.services.lineup import LineupService


def _make_bot(guilds: list | None = None, channel: MagicMock | None = None) -> MagicMock:
    bot = MagicMock()
    if guilds is None:
        guild = MagicMock()
        guild.id = 123456789
        bot.guilds = [guild]
    else:
        bot.guilds = guilds
    bot.get_channel = MagicMock(return_value=channel or _make_channel())
    return bot


def _make_channel() -> MagicMock:
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=999))
    channel.fetch_message = AsyncMock(return_value=MagicMock(id=888, edit=AsyncMock()))
    return channel


def _make_service(
    bot: MagicMock | None = None,
    db: MagicMock | None = None,
    log_service: MagicMock | None = None,
) -> LineupService:
    return LineupService(
        bot=bot or _make_bot(),
        db=db or MagicMock(),
        log_service=log_service or MagicMock(post_error=AsyncMock()),
    )


def _make_events() -> list[dict]:
    return [
        {
            "id": "page-1",
            "last_edited_time": "2026-06-01T00:00:00.000Z",
            "properties": {
                "Name": {"title": [{"plain_text": "Friday Night Rave"}]},
                "Date": {"date": {"start": "2026-06-13T20:00:00+00:00"}},
                "Slots": {"number": 6},
                "DJs": {"relation": [{"id": "dj-1"}, {"id": "dj-2"}]},
            },
        }
    ]


@pytest.mark.asyncio
async def test_post_update_posts_when_no_prior_post() -> None:
    """No prior post today → channel.send called, config keys persisted."""
    channel = _make_channel()
    bot = _make_bot(channel=channel)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock) as mock_set,
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": "2000-01-01",
            "lineup_post_message_id": None,
        }.get(key)

        await service.post_update(_make_events())

    channel.send.assert_awaited_once()
    assert mock_set.await_count == 2


@pytest.mark.asyncio
async def test_post_update_edits_even_when_posted_today() -> None:
    """lineup_last_post_date == today → the existing message is still edited (no freeze)."""
    today = datetime.date.today().isoformat()
    existing_msg = MagicMock(id=888)
    existing_msg.edit = AsyncMock()
    channel = _make_channel()
    channel.fetch_message = AsyncMock(return_value=existing_msg)
    bot = _make_bot(channel=channel)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock),
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": today,
            "lineup_post_message_id": "888",
        }.get(key)

        assert await service.post_update(_make_events()) is True

    existing_msg.edit.assert_awaited_once()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_update_new_post_capped_per_day_reports_not_done() -> None:
    """No editable message and a new post already sent today → nothing sent, returns False."""
    today = datetime.date.today().isoformat()
    channel = _make_channel()
    bot = _make_bot(channel=channel)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock) as mock_set,
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": today,
        }.get(key)

        assert await service.post_update(_make_events()) is False

    channel.send.assert_not_awaited()
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_update_edits_existing_message() -> None:
    """Prior msg_id exists → channel.fetch_message + msg.edit called."""
    existing_msg = MagicMock(id=888)
    existing_msg.edit = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=999))
    channel.fetch_message = AsyncMock(return_value=existing_msg)
    bot = _make_bot(channel=channel)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock),
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": "2000-01-01",
            "lineup_post_message_id": "888",
        }.get(key)

        await service.post_update(_make_events())

    channel.fetch_message.assert_awaited_once_with(888)
    existing_msg.edit.assert_awaited_once()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_update_deletes_and_reposts_on_not_found() -> None:
    """fetch_message raises NotFound → channel.send called, old msg id overwritten."""
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=999))
    channel.fetch_message = AsyncMock(
        side_effect=disnake.NotFound(MagicMock(status=404, reason="Not Found"), "Not Found")
    )
    bot = _make_bot(channel=channel)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock) as mock_set,
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": "2000-01-01",
            "lineup_post_message_id": "888",
        }.get(key)

        await service.post_update(_make_events())

    channel.send.assert_awaited_once()
    assert mock_set.await_count == 2


@pytest.mark.asyncio
async def test_post_update_logs_error_when_channel_not_configured() -> None:
    """dj_channel is None → log_service.post_error called, no send."""
    channel = _make_channel()
    bot = _make_bot(channel=channel)
    log_service = MagicMock(post_error=AsyncMock())
    service = _make_service(bot=bot, log_service=log_service)

    with patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = None

        await service.post_update(_make_events())

    log_service.post_error.assert_awaited_once()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_update_no_guilds_returns_early() -> None:
    """bot.guilds empty → returns without posting."""
    channel = _make_channel()
    bot = _make_bot(guilds=[], channel=channel)
    service = _make_service(bot=bot)

    with patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get:
        await service.post_update(_make_events())

    mock_get.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_update_returns_early_when_channel_not_in_cache() -> None:
    """bot.get_channel() returns None → returns without posting."""
    bot = _make_bot()
    bot.get_channel = MagicMock(return_value=None)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock) as mock_set,
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": "2000-01-01",
            "lineup_post_message_id": None,
        }.get(key)

        await service.post_update(_make_events())

    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_update_reposts_on_forbidden_fetch() -> None:
    """fetch_message raises Forbidden → channel.send called as fallback."""
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=999))
    channel.fetch_message = AsyncMock(
        side_effect=disnake.Forbidden(MagicMock(status=403, reason="Forbidden"), "Forbidden")
    )
    bot = _make_bot(channel=channel)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock) as mock_set,
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": "2000-01-01",
            "lineup_post_message_id": "888",
        }.get(key)

        await service.post_update(_make_events())

    channel.send.assert_awaited_once()
    assert mock_set.await_count == 2


@pytest.mark.asyncio
async def test_post_update_deletes_old_and_reposts_when_edit_forbidden() -> None:
    """fetch_message succeeds but edit raises Forbidden → old deleted, new sent (AC3)."""
    existing_msg = MagicMock(id=888)
    existing_msg.edit = AsyncMock(
        side_effect=disnake.Forbidden(MagicMock(status=403, reason="Forbidden"), "Forbidden")
    )
    existing_msg.delete = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=999))
    channel.fetch_message = AsyncMock(return_value=existing_msg)
    bot = _make_bot(channel=channel)
    service = _make_service(bot=bot)

    with (
        patch("bot.services.lineup.get_value", new_callable=AsyncMock) as mock_get,
        patch("bot.services.lineup.set_value", new_callable=AsyncMock) as mock_set,
    ):
        mock_get.side_effect = lambda db, guild_id, key: {
            "dj_channel": "111",
            "lineup_last_post_date": "2000-01-01",
            "lineup_post_message_id": "888",
        }.get(key)

        await service.post_update(_make_events())

    existing_msg.delete.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert mock_set.await_count == 2
