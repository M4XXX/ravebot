"""Tests for bot/cogs/onboarding.py — DJ role detection and DM initiation."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import disnake
import pytest

from bot.ui import texts
from bot.ui.embeds import ErrorCode
from tests.conftest import make_real_ctx


def make_role(name: str) -> MagicMock:
    role = MagicMock(spec=disnake.Role)
    role.name = name
    role.__hash__ = lambda self: hash(name)
    role.__eq__ = lambda self, other: self.name == getattr(other, "name", None)
    return role


def make_member(user_id: int = 12345, name: str = "TestDJ") -> MagicMock:
    member = MagicMock(spec=disnake.Member)
    member.id = user_id
    member.name = name
    member.send = AsyncMock()
    member.guild = MagicMock()
    member.guild.id = 99999
    member.guild.roles = [make_role("DJ")]
    return member


def make_ctx(db: object = None, log: object = None) -> MagicMock:
    ctx = MagicMock()
    ctx.db = db
    ctx.log_service = log
    return ctx


@pytest.mark.asyncio
async def test_on_member_update_ignores_no_new_roles() -> None:
    """No DM sent when the member update has no new roles added."""
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(make_ctx())
    role = make_role("DJ")
    before = make_member()
    before.roles = [role]
    after = make_member()
    after.roles = [role]  # same roles — no change

    await cog.on_member_update(before, after)

    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_update_ignores_non_dj_role() -> None:
    """No DM sent when a non-DJ role is newly assigned."""
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(make_ctx())
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("Moderator")]

    await cog.on_member_update(before, after)

    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_update_skips_existing_dj() -> None:
    """No DM sent when the DJ already exists in the database."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_ctx(db=MagicMock())
    cog = OnboardingCog(ctx)
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("DJ")]

    existing_record = MagicMock()
    with patch(
        "bot.cogs.onboarding.get_dj_by_discord_id",
        new=AsyncMock(return_value=existing_record),
    ):
        await cog.on_member_update(before, after)

    after.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_member_update_sends_dm_to_new_dj() -> None:
    """DM sent with correct message text when a new DJ role is detected."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_ctx(db=MagicMock())
    cog = OnboardingCog(ctx)
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("DJ")]

    with patch(
        "bot.cogs.onboarding.get_dj_by_discord_id",
        new=AsyncMock(return_value=None),
    ):
        await cog.on_member_update(before, after)

    after.send.assert_awaited_once()
    sent_content = after.send.call_args[0][0]
    from bot.ui import texts

    assert sent_content == texts.WELCOME
    assert "Active DJ Booking Group" in sent_content


@pytest.mark.asyncio
async def test_on_member_update_posts_error_on_dm_failure() -> None:
    """ERR_DM_FAILED posted to admin log when DM is blocked; no exception raised."""
    from bot.cogs.onboarding import OnboardingCog

    log = MagicMock()
    log.post_error = AsyncMock()
    ctx = make_ctx(db=MagicMock(), log=log)
    cog = OnboardingCog(ctx)
    before = make_member()
    before.roles = []
    after = make_member()
    after.roles = [make_role("DJ")]
    after.display_name = "DJ Anna"
    after.send = AsyncMock(
        side_effect=disnake.Forbidden(MagicMock(), "Cannot send messages to this user")
    )

    with patch(
        "bot.cogs.onboarding.get_dj_by_discord_id",
        new=AsyncMock(return_value=None),
    ):
        await cog.on_member_update(before, after)  # must not raise

    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_DM_FAILED


# ---------------------------------------------------------------------------
# Welcome DM: one shared intro view (no per-DM view growth)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_member_update_reuses_one_intro_view() -> None:
    """Every welcome DM carries the same persistent OnboardingIntroView instance."""
    from bot.cogs.onboarding import OnboardingCog
    from bot.ui.dj_profile import OnboardingIntroView

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx)
    views = []
    for user_id in (55555, 66666):
        before = make_member()
        before.roles = []
        after = make_member(user_id=user_id)
        after.roles = [make_role("DJ")]
        with patch(
            "bot.cogs.onboarding.get_dj_by_discord_id",
            new=AsyncMock(return_value=None),
        ):
            await cog.on_member_update(before, after)
        after.send.assert_awaited_once()
        assert after.send.call_args.args[0] == texts.WELCOME
        views.append(after.send.call_args.kwargs["view"])

    assert isinstance(views[0], OnboardingIntroView)
    assert views[0] is views[1]
    assert views[0].ctx is ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(("row", "onboarded"), [(None, False), ({"active_booking": True}, True)])
async def test_on_message_menu_offers_my_profile_only_to_onboarded(
    monkeypatch: pytest.MonkeyPatch, row: object, onboarded: bool
) -> None:
    from bot.cogs.onboarding import OnboardingCog

    monkeypatch.setattr("bot.cogs.onboarding.get_dj_by_discord_id", AsyncMock(return_value=row))
    cog = OnboardingCog(make_real_ctx(db=MagicMock()))
    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 1
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    view = message.channel.send.call_args.kwargs["view"]
    assert ("fallback:my_profile" in [c.custom_id for c in view.children]) is onboarded


# ---------------------------------------------------------------------------
# Story 2.4: on_message + FallbackMenuView trigger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_message_dm_sends_fallback_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Free-text DM outside active flow sends FallbackMenuView."""
    from bot.cogs.onboarding import OnboardingCog
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx)

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 11111
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_awaited_once()
    send_kwargs = message.channel.send.call_args.kwargs
    assert isinstance(send_kwargs.get("view"), FallbackMenuView)


@pytest.mark.asyncio
async def test_on_message_guild_message_ignored() -> None:
    """Messages in guild channels do not trigger FallbackMenuView."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx)

    message = MagicMock(spec=disnake.Message)
    message.guild = MagicMock()  # guild message
    message.author.id = 22222
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_bot_message_ignored() -> None:
    """Bot's own DMs do not trigger FallbackMenuView."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx)

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 33333
    message.author.bot = True
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_skips_during_active_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """FallbackMenuView NOT shown when user has an active flow (lock held)."""
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_real_ctx(db=MagicMock())
    cog = OnboardingCog(ctx)

    await ctx.get_user_lock(44444).acquire()

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 44444
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    message.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_dm_logs_transcript_for_incoming_and_outgoing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 6.1 AC2 + AC1: incoming DM and fallback menu response are both transcribed."""
    from bot.cogs.onboarding import OnboardingCog

    log_service = MagicMock()
    ctx = make_real_ctx(db=MagicMock())
    ctx.log_service = log_service

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 55555
    message.author.name = "DJ Tester"
    message.author.bot = False
    message.content = "hello bot"
    message.channel.send = AsyncMock()

    cog = OnboardingCog(ctx)
    await cog.on_message(message)

    message.channel.send.assert_awaited_once()
    assert message.channel.send.call_args.args[0] == texts.MENU


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, texts.BTN_INVITES_ON),
        ({"active_booking": False}, texts.BTN_INVITES_ON),
        ({"active_booking": True}, texts.BTN_INVITES_OFF),
    ],
)
async def test_on_message_menu_matches_dj_state(
    monkeypatch: pytest.MonkeyPatch, row: object, expected: str
) -> None:
    from bot.cogs.onboarding import OnboardingCog

    monkeypatch.setattr("bot.cogs.onboarding.get_dj_by_discord_id", AsyncMock(return_value=row))
    cog = OnboardingCog(make_real_ctx(db=MagicMock()))
    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 1
    message.author.bot = False
    message.channel.send = AsyncMock()

    await cog.on_message(message)

    view = message.channel.send.call_args.kwargs["view"]
    labels = [c.label for c in view.children]
    assert expected in labels
    assert message.channel.send.call_args[0][0] == texts.MENU


# ---------------------------------------------------------------------------
# The menu is always sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_message_always_shows_fallback_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every DJ message gets the FallbackMenuView (no suppression path)."""
    from bot.cogs.onboarding import OnboardingCog
    from bot.ui.views import FallbackMenuView

    ctx = make_real_ctx(db=MagicMock())

    message = MagicMock(spec=disnake.Message)
    message.guild = None
    message.author.id = 77
    message.author.name = "DJ Free"
    message.author.bot = False
    message.content = "hello"
    message.channel.send = AsyncMock()

    cog = OnboardingCog(ctx)
    await _send(cog, message)

    message.channel.send.assert_awaited_once()
    view = message.channel.send.call_args.kwargs.get("view")
    assert isinstance(view, FallbackMenuView)


# ---------------------------------------------------------------------------
# Log channel: every typed DM is logged first
# ---------------------------------------------------------------------------


def _log_service() -> MagicMock:
    log_service = MagicMock()
    logged = MagicMock()
    logged.id = 777
    log_service.log_user_message = AsyncMock(return_value=logged)
    return log_service


def _dm(content: str = "hello", attachments: list | None = None, user_id: int = 5,
        name: str = "Sam", bot: bool = False, guild: object = None) -> MagicMock:
    message = MagicMock(spec=disnake.Message)
    message.guild = guild
    message.author.id = user_id
    message.author.name = name
    message.author.display_name = name
    message.author.bot = bot
    message.content = content
    message.attachments = attachments or []
    message.channel.send = AsyncMock()
    return message


async def _send(cog, message) -> None:
    """Run on_message, then wait for the background log task it scheduled."""
    await cog.on_message(message)
    if cog._tasks:
        await asyncio.gather(*list(cog._tasks))


def _cog(log_service: MagicMock):
    from bot.cogs.onboarding import OnboardingCog

    ctx = make_real_ctx(db=MagicMock())
    ctx.log_service = log_service
    return OnboardingCog(ctx)


@pytest.fixture
def _plain(monkeypatch: pytest.MonkeyPatch) -> dict:
    mocks = {
        "dj": AsyncMock(return_value=None),
        "save": AsyncMock(),
    }
    monkeypatch.setattr("bot.cogs.onboarding.get_dj_by_discord_id", mocks["dj"])
    monkeypatch.setattr("bot.cogs.onboarding.save_log_reply", mocks["save"])
    return mocks


@pytest.mark.asyncio
async def test_on_message_logs_plain_text_saves_mapping_and_sends_menu(_plain: dict) -> None:
    from bot.ui.log_reply import LogReplyView

    log_service = _log_service()
    msg = _dm(content="hi there")
    await _send(_cog(log_service), msg)

    log_service.log_user_message.assert_awaited_once()
    args = log_service.log_user_message.await_args.args
    assert args[:4] == (msg.author, "hi there", [], None)
    assert isinstance(args[4], LogReplyView)
    _plain["save"].assert_awaited_once()
    assert _plain["save"].await_args.args[1:] == (777, 5)
    msg.channel.send.assert_awaited_once()
    assert msg.channel.send.call_args.args[0] == texts.MENU


@pytest.mark.asyncio
async def test_on_message_logs_attachment_only(_plain: dict) -> None:
    att = MagicMock()
    att.filename = "flyer.png"
    log_service = _log_service()
    msg = _dm(content="", attachments=[att])
    await _send(_cog(log_service), msg)
    args = log_service.log_user_message.await_args.args
    assert args[1] == "" and args[2] == ["flyer.png"]
    msg.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_message_empty_without_attachments_logs_nothing(_plain: dict) -> None:
    log_service = _log_service()
    await _send(_cog(log_service), _dm(content=""))
    log_service.log_user_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_flow_lock_logs_but_no_menu(_plain: dict) -> None:
    log_service = _log_service()
    cog = _cog(log_service)
    await cog.ctx.get_user_lock(5).acquire()
    msg = _dm()
    await _send(cog, msg)
    log_service.log_user_message.assert_awaited_once()
    msg.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_bot_and_guild_log_nothing(_plain: dict) -> None:
    log_service = _log_service()
    cog = _cog(log_service)
    await _send(cog, _dm(bot=True))
    await _send(cog, _dm(guild=MagicMock()))
    log_service.log_user_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_no_mapping_when_post_failed(_plain: dict) -> None:
    log_service = _log_service()
    log_service.log_user_message = AsyncMock(return_value=None)
    msg = _dm()
    await _send(_cog(log_service), msg)
    _plain["save"].assert_not_awaited()
    msg.channel.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected"),
    [({"dj_name": "DJ Sam", "active_booking": False}, "DJ Sam"),
     ({"dj_name": "Sam", "active_booking": False}, None), (None, None)],
)
async def test_on_message_passes_dj_name_only_when_different(
    _plain: dict, row: object, expected: object
) -> None:
    _plain["dj"].return_value = row
    log_service = _log_service()
    await _send(_cog(log_service), _dm(name="Sam"))
    assert log_service.log_user_message.await_args.args[3] == expected


@pytest.mark.asyncio
async def test_on_message_dj_lookup_failure_gives_none_and_still_logs(_plain: dict) -> None:
    _plain["dj"].side_effect = RuntimeError("db")
    log_service = _log_service()
    msg = _dm()
    await _send(_cog(log_service), msg)
    assert log_service.log_user_message.await_args.args[3] is None
    msg.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_is_not_delayed_by_a_slow_log_post(_plain: dict) -> None:
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()
        return None

    log_service = _log_service()
    log_service.log_user_message = AsyncMock(side_effect=slow)
    cog = _cog(log_service)
    msg = _dm()
    await cog.on_message(msg)
    msg.channel.send.assert_awaited_once()
    gate.set()
    await asyncio.gather(*list(cog._tasks))
    log_service.log_user_message.assert_awaited_once()
    assert not cog._tasks


@pytest.mark.asyncio
async def test_failing_log_task_is_swallowed(_plain: dict) -> None:
    log_service = _log_service()
    log_service.log_user_message = AsyncMock(side_effect=RuntimeError("boom"))
    cog = _cog(log_service)
    msg = _dm()
    await _send(cog, msg)
    msg.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_dms_share_one_reply_view(_plain: dict) -> None:
    log_service = _log_service()
    cog = _cog(log_service)
    await _send(cog, _dm(content="one"))
    await _send(cog, _dm(content="two"))
    views = [c.args[4] for c in log_service.log_user_message.await_args_list]
    assert len(views) == 2
    assert views[0] is views[1]


def _stored_dj_role(role_id: str):
    return patch("bot.services.roles.get_value", AsyncMock(return_value=role_id))


@pytest.mark.asyncio
async def test_on_member_update_uses_stored_dj_role() -> None:
    """Onboarding starts for the stored DJ role, not for a role named DJ."""
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(make_ctx(db=MagicMock()))
    performers = make_role("Performers")
    performers.id = 7
    before, after = make_member(), make_member()
    after.guild.roles = [make_role("DJ"), performers]
    after.guild.get_role = lambda rid: performers if rid == 7 else None
    before.roles = []
    after.roles = [performers]

    with _stored_dj_role("7"), patch(
        "bot.cogs.onboarding.get_dj_by_discord_id", new=AsyncMock(return_value=None)
    ):
        await cog.on_member_update(before, after)

    after.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_member_update_ignores_old_name_when_stored_role_differs() -> None:
    from bot.cogs.onboarding import OnboardingCog

    cog = OnboardingCog(make_ctx(db=MagicMock()))
    performers = make_role("Performers")
    performers.id = 7
    before, after = make_member(), make_member()
    after.guild.get_role = lambda rid: performers if rid == 7 else None
    before.roles = []
    after.roles = [make_role("DJ")]

    with _stored_dj_role("7"):
        await cog.on_member_update(before, after)

    after.send.assert_not_called()
