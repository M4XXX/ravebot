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
        assert sent_embed.title == "Database unavailable"

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


# --- Typed entries (group 4) ---------------------------------------------------

import datetime  # noqa: E402

import disnake  # noqa: E402

from tests.notion.factories import make_event  # noqa: E402


def _ev():
    return make_event(
        event_id="EV-1",
        date=datetime.date(2026, 11, 14),
        start=datetime.time(18, 0),
        slot_minutes=60,
        slot_count=4,
    )


def _service(channel=None, **kw):
    from bot.log import AdminLogService

    channel = channel if channel is not None else _make_mock_channel()
    return AdminLogService(bot=_make_mock_bot(channel=channel), db=_make_mock_pool("111")), channel


def _sent(channel):
    channel.send.assert_awaited_once()
    kw = channel.send.call_args.kwargs
    assert kw["allowed_mentions"].everyone is False
    assert kw["allowed_mentions"].users is False
    return kw["embed"]


GREEN, GOLD, RED, BLUE = 0x2E7D32, 0xFFD700, 0xED4245, 0x5865F2


@pytest.mark.parametrize(
    ("call", "title", "body", "color"),
    [
        (lambda s: s.log_booked("Nova", _ev(), 1, "DM"), "Booked",
         "Nova booked **EV-1**, slot 2 at 19:00 (via DM)", GREEN),
        (lambda s: s.log_booked("Nova", _ev(), 1, "Notion"), "Booked",
         "Nova booked **EV-1**, slot 2 at 19:00 (added in Notion)", GREEN),
        (lambda s: s.log_cancelled("Nova", _ev(), 0), "Cancelled",
         "Nova cancelled **EV-1**, slot 1 at 18:00 (via DM)", GOLD),
        (lambda s: s.log_changed_by_organizer("Max", "booked", "Nova", _ev(), 2),
         "Changed by an organizer", "Max booked Nova into **EV-1**, slot 3 at 20:00", GOLD),
        (lambda s: s.log_changed_by_organizer("Max", "removed", "Nova", _ev(), 2),
         "Changed by an organizer", "Max removed Nova from **EV-1**, slot 3 at 20:00", GOLD),
        (lambda s: s.log_changed_in_notion("Nova", _ev(), 3), "Changed in Notion",
         "Nova was removed from **EV-1**, slot 4 at 21:00", GOLD),
        (lambda s: s.log_fully_booked(_ev()), "Event fully booked",
         "**EV-1**: all 4 slots are booked.", GREEN),
        (lambda s: s.log_new_dj("Nova", "NovaVR"), "New DJ",
         "Nova (VRChat: NovaVR) joined the DJ pool.", GREEN),
        (lambda s: s.log_new_dj("Nova", None), "New DJ", "Nova joined the DJ pool.", GREEN),
    ],
)
async def test_typed_entries(call, title, body, color) -> None:
    service, channel = _service()
    await call(service)
    embed = _sent(channel)
    assert (embed.title, embed.description, embed.colour.value) == (title, body, color)
    assert embed.footer.text == "RaveBot"


async def test_mentions_in_names_are_neutralised() -> None:
    service, channel = _service()
    await service.log_booked("@everyone <@1> *x*", _ev(), 0, "DM")
    embed = _sent(channel)
    assert "@everyone" not in embed.description
    assert "<@1>" not in embed.description
    assert "\\*x\\*" in embed.description


async def test_attention_has_todo_field() -> None:
    service, channel = _service()
    await service.log_attention("Something failed", "It broke.", "Fix it.")
    embed = _sent(channel)
    assert embed.colour.value == RED
    assert (embed.fields[0].name, embed.fields[0].value) == ("What to do", "Fix it.")
    service, channel = _service()
    await service.log_attention("Something failed", "It broke.")
    assert _sent(channel).fields == []


async def test_no_channel_warns_and_does_not_raise(caplog) -> None:
    from bot.log import AdminLogService

    service = AdminLogService(bot=_make_mock_bot(has_guilds=False), db=_make_mock_pool())
    with caplog.at_level(logging.WARNING, logger="bot.log"):
        await service.log_fully_booked(_ev())
        assert await service.log_user_message(_user(), "hi", [], None) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_http_exception_does_not_raise() -> None:
    channel = _make_mock_channel()
    channel.send = AsyncMock(side_effect=disnake.HTTPException(MagicMock(), "fail"))
    service, _ = _service(channel)
    await service.log_new_dj("Nova", None)


@pytest.mark.parametrize(
    ("code", "title"),
    [
        ("ERR_DM_FAILED", "Could not DM a DJ"),
        ("ERR_NOTION_WRITE", "Notion write failed"),
        ("ERR_DB_UNAVAILABLE", "Database unavailable"),
        ("ERR_OUTREACH_EXHAUSTED", "No more DJs to ask"),
    ],
)
async def test_post_error_mapping(code, title) -> None:
    from bot.ui.embeds import ErrorCode

    service, channel = _service()
    await service.post_error(ErrorCode(code), "Trigger text", "Did a thing.", "Do this")
    embed = _sent(channel)
    assert embed.title == title
    assert embed.description == "Trigger text. Did a thing."
    assert embed.fields[0].value == "Do this"


async def test_post_error_default_manual_action_and_empty_action() -> None:
    from bot.ui.embeds import ErrorCode

    service, channel = _service()
    await service.post_error(ErrorCode.ERR_DM_FAILED, "Trigger text", "")
    embed = _sent(channel)
    assert embed.description == "Trigger text"
    assert embed.fields == []


@pytest.mark.parametrize("code", ["ERR_SLOT_CONFLICT", "ERR_BUTTON_TIMEOUT"])
async def test_post_error_not_posted_for_logger_only_codes(code) -> None:
    from bot.ui.embeds import ErrorCode

    service, channel = _service()
    await service.post_error(ErrorCode(code), "t", "a")
    channel.send.assert_not_awaited()


def _user(display="Nova", name="nova"):
    user = MagicMock()
    user.display_name = display
    user.name = name
    return user


async def test_user_message_returns_message_and_formats() -> None:
    channel = _make_mock_channel()
    sent = MagicMock()
    channel.send = AsyncMock(return_value=sent)
    service, _ = _service(channel)
    view = MagicMock()
    result = await service.log_user_message(
        _user(), "hello @everyone", ["a.png"], "DJ Nova", view
    )
    assert result is sent
    kw = channel.send.call_args.kwargs
    assert kw["view"] is view
    embed = kw["embed"]
    assert embed.title == "Message from Nova (DJ DJ Nova)"
    assert "@everyone" not in embed.description
    assert embed.description.endswith("\n(attachment: a.png)")
    assert embed.colour.value == BLUE


async def test_user_message_title_without_dj_or_same_name() -> None:
    service, channel = _service()
    await service.log_user_message(_user(), "x", [], "Nova")
    assert _sent(channel).title == "Message from Nova"


async def test_user_message_empty_text_only_attachments() -> None:
    service, channel = _service()
    await service.log_user_message(_user(), "", ["a.png", "b.mp3"], None)
    assert _sent(channel).description == "(attachment: a.png)\n(attachment: b.mp3)"


async def test_user_message_long_text_is_cut() -> None:
    service, channel = _service()
    await service.log_user_message(_user(), "x" * 5000, [], None)
    embed = _sent(channel)
    assert len(embed.description) <= 4096
    assert embed.description.endswith("(cut)")


@pytest.mark.asyncio
async def test_building_failure_does_not_raise(caplog) -> None:
    service, channel = _service()
    broken_event = object()  # no event_id / slots: building the text raises
    await service.log_booked("Nova", broken_event, 0, "DM")  # type: ignore[arg-type]
    await service.log_cancelled("Nova", broken_event, 0)  # type: ignore[arg-type]
    await service.log_fully_booked(broken_event)  # type: ignore[arg-type]
    channel.send.assert_not_awaited()
