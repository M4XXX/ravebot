"""Tests for bot/log.py — AdminLogService."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_mock_channel() -> MagicMock:
    """Return a mock disnake.TextChannel that supports await channel.send(...)."""
    import disnake

    channel = AsyncMock(spec=disnake.TextChannel)
    return channel


def _make_mock_bot(has_guilds: bool = True, channel: object = None) -> MagicMock:
    """Build a minimal mock disnake.Bot."""
    bot = MagicMock()
    guild = MagicMock()
    guild.id = 999
    bot.guilds = [guild] if has_guilds else []
    bot.get_channel = MagicMock(return_value=channel)
    return bot


def _make_mock_pool(log_channel_value: str | None = "111") -> MagicMock:
    """Return a mock asyncpg.Pool whose fetchrow returns a log_channel row."""
    pool = MagicMock()
    row = {"value": log_channel_value} if log_channel_value is not None else None
    pool.fetchrow = AsyncMock(return_value=row)
    return pool


class TestAdminLogServicePostError:
    @pytest.mark.asyncio
    async def test_sends_error_embed_to_channel(self) -> None:
        """post_error() sends an ErrorEmbed to the configured log channel."""
        mock_channel = _make_mock_channel()
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="111")
        from bot.log import AdminLogService
        from bot.ui.embeds import ErrorCode

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        await service.post_error(
            ErrorCode.ERR_DM_FAILED,
            "DJ blocked DMs",
            "Logged failure",
            "Check DJ settings",
        )

        mock_channel.send.assert_awaited_once()
        kwargs = mock_channel.send.call_args.kwargs
        assert "embed" in kwargs

    @pytest.mark.asyncio
    async def test_embed_title_matches_error_code(self) -> None:
        """The sent embed title is the error code string."""
        mock_channel = _make_mock_channel()
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="111")
        from bot.log import AdminLogService
        from bot.ui.embeds import ErrorCode

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        await service.post_error(ErrorCode.ERR_DB_UNAVAILABLE, "DB down", "Paused")

        sent_embed = mock_channel.send.call_args.kwargs["embed"]
        assert sent_embed.title == "ERR_DB_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_falls_back_to_stdout_when_no_guilds(self, caplog) -> None:
        """post_error() logs a warning (not error) when bot has no guilds yet."""
        mock_bot = _make_mock_bot(has_guilds=False)
        mock_pool = _make_mock_pool()
        from bot.log import AdminLogService
        from bot.ui.embeds import ErrorCode

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        with caplog.at_level(logging.WARNING, logger="bot.log"):
            await service.post_error(
                ErrorCode.ERR_DB_UNAVAILABLE, "DB down", "Paused scheduler"
            )

        # Must not raise; must not log at ERROR level
        assert not any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.asyncio
    async def test_falls_back_to_stdout_when_no_channel_configured(self, caplog) -> None:
        """post_error() logs a warning when config has no log_channel set."""
        mock_bot = _make_mock_bot(channel=None)
        mock_pool = _make_mock_pool(log_channel_value=None)
        from bot.log import AdminLogService
        from bot.ui.embeds import ErrorCode

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        with caplog.at_level(logging.WARNING, logger="bot.log"):
            await service.post_error(ErrorCode.ERR_NOTION_WRITE, "Write failed", "Retried")

        assert not any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.asyncio
    async def test_does_not_raise_on_discord_exception(self) -> None:
        """post_error() catches DiscordException and logs error instead of raising."""
        import disnake

        mock_channel = _make_mock_channel()
        mock_channel.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(), "fail"))
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="111")
        from bot.log import AdminLogService
        from bot.ui.embeds import ErrorCode

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        # Must not raise
        await service.post_error(ErrorCode.ERR_DM_FAILED, "trigger", "action")


class TestAdminLogServicePostInteraction:
    @pytest.mark.asyncio
    async def test_sends_admin_log_embed_to_channel(self) -> None:
        """post_interaction() sends an AdminLogEmbed to the configured log channel."""
        mock_channel = _make_mock_channel()
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="222")
        from bot.log import AdminLogService

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        await service.post_interaction("DJ Felix", "Booking", "Slot secured")

        mock_channel.send.assert_awaited_once()
        kwargs = mock_channel.send.call_args.kwargs
        assert "embed" in kwargs

    @pytest.mark.asyncio
    async def test_embed_color_is_nebula_white(self) -> None:
        """AdminLogEmbed posted by post_interaction() uses NEBULA_WHITE color."""
        mock_channel = _make_mock_channel()
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="222")
        from bot.log import AdminLogService
        from bot.ui.embeds import EmbedColors

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        await service.post_interaction("DJ Mars", "Onboarding", "Complete")

        sent_embed = mock_channel.send.call_args.kwargs["embed"]
        assert sent_embed.color.value == EmbedColors.NEBULA_WHITE

    @pytest.mark.asyncio
    async def test_falls_back_to_stdout_when_no_guilds(self, caplog) -> None:
        """post_interaction() logs a warning when bot has no guilds."""
        mock_bot = _make_mock_bot(has_guilds=False)
        mock_pool = _make_mock_pool()
        from bot.log import AdminLogService

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        with caplog.at_level(logging.WARNING, logger="bot.log"):
            await service.post_interaction("DJ Mars", "Onboarding", "Complete")

        assert not any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.asyncio
    async def test_does_not_raise_on_discord_exception(self) -> None:
        """post_interaction() catches DiscordException and logs error instead of raising."""
        import disnake

        mock_channel = _make_mock_channel()
        mock_channel.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(), "fail"))
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="222")
        from bot.log import AdminLogService

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        # Must not raise
        await service.post_interaction("DJ Felix", "Booking", "Slot secured")


class TestAdminLogServicePostIntervention:
    @pytest.mark.asyncio
    async def test_sends_plain_message_to_channel(self) -> None:
        """post_intervention() sends a plain text message (no embed)."""
        mock_channel = _make_mock_channel()
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="333")
        from bot.log import AdminLogService

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        await service.post_intervention("BOT", "@DJ Felix", "Hello, you're booked!")

        mock_channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_message_contains_sender_and_recipient(self) -> None:
        """Intervention message includes sender and recipient names."""
        mock_channel = _make_mock_channel()
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="333")
        from bot.log import AdminLogService

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        await service.post_intervention("Admin", "@DJ Felix", "Check your slot time")

        sent_text = mock_channel.send.call_args.args[0]
        assert "Admin" in sent_text
        assert "@DJ Felix" in sent_text

    @pytest.mark.asyncio
    async def test_falls_back_to_stdout_when_no_channel(self, caplog) -> None:
        """post_intervention() logs a warning when no channel is configured."""
        mock_bot = _make_mock_bot(has_guilds=False)
        mock_pool = _make_mock_pool()
        from bot.log import AdminLogService

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        with caplog.at_level(logging.WARNING, logger="bot.log"):
            await service.post_intervention("BOT", "@DJ Mars", "Reminder sent")

        assert not any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.asyncio
    async def test_does_not_raise_on_discord_exception(self) -> None:
        """post_intervention() catches DiscordException and logs error instead of raising."""
        import disnake

        mock_channel = _make_mock_channel()
        mock_channel.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(), "fail"))
        mock_bot = _make_mock_bot(channel=mock_channel)
        mock_pool = _make_mock_pool(log_channel_value="333")
        from bot.log import AdminLogService

        service = AdminLogService(bot=mock_bot, db=mock_pool)
        # Must not raise
        await service.post_intervention("Admin", "@DJ Mars", "Check your slot")
