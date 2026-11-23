"""Tests for bot/cogs/announcements_cog.py (/announcements commands and loop)."""
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db.announcements import AnnouncementRule
from bot.services.announcement_settings import AnnouncementSettings
from bot.services.announcement_template import DEFAULT_TEMPLATES, GENERAL, START

T = datetime.time
CREATED = datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC)
SET = "bot.cogs.announcements_cog.settings"
DB = "bot.cogs.announcements_cog.db_ann"


def _rule(rid=1, days=3, at=T(18, 0)) -> AnnouncementRule:
    return AnnouncementRule(rid, days, at, CREATED)


def _settings(**kw) -> AnnouncementSettings:
    base = {
        "channel_id": None,
        "general_enabled": False,
        "start_enabled": False,
        "general_template": DEFAULT_TEMPLATES[GENERAL],
        "start_template": DEFAULT_TEMPLATES[START],
        "general_since": None,
    }
    base.update(kw)
    return AnnouncementSettings(**base)


def _ctx(admins=frozenset({1})) -> MagicMock:
    ctx = MagicMock()
    ctx.is_admin = lambda uid: uid in admins
    ctx.maintenance = None
    ctx.db = MagicMock()
    ctx.announcement_service = MagicMock()
    ctx.announcement_service.tick = AsyncMock()
    return ctx


def _inter(uid: int = 1):
    inter = MagicMock()
    inter.author.id = uid
    inter.guild_id = 111
    inter.response = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _cog(ctx):
    from bot.cogs.announcements_cog import AnnouncementsCog

    return AnnouncementsCog(ctx)


def _text(inter) -> str:
    if inter.followup.send.call_args is not None:
        args, kwargs = inter.followup.send.call_args
    else:
        args, kwargs = inter.response.send_message.call_args
    assert kwargs["ephemeral"] is True
    return args[0]


def _commands(cog):
    return {
        "show": lambda i: cog.ann_show.callback(cog, i),
        "channel": lambda i: cog.ann_channel.callback(cog, i, None),
        "general": lambda i: cog.ann_general.callback(cog, i, "on"),
        "start": lambda i: cog.ann_start.callback(cog, i, "on"),
        "rule_add": lambda i: cog.rule_add.callback(cog, i, 3, "18:00"),
        "rule_edit": lambda i: cog.rule_edit.callback(cog, i, 1, 2, None),
        "rule_remove": lambda i: cog.rule_remove.callback(cog, i, 1),
    }


@pytest.mark.asyncio
async def test_non_admin_refused_for_every_sub_command() -> None:
    cog = _cog(_ctx())
    assert await cog.cog_slash_command_check(_inter(uid=99)) is False
    assert await cog.cog_slash_command_check(_inter(uid=1)) is True
    names = {c.qualified_name for c in cog.get_slash_commands()}
    assert names == {"announcements"}
    group = cog.get_slash_commands()[0]
    assert set(group.children) == {"show", "channel", "general", "start", "rule"}
    assert set(group.children["rule"].children) == {"add", "edit", "remove"}


@pytest.mark.asyncio
async def test_show_lists_channel_switches_rules_and_templates_state() -> None:
    cog = _cog(_ctx())
    inter = _inter()
    s = _settings(channel_id=77, general_enabled=True, start_template="custom {event}")
    with (
        patch(f"{SET}.load_settings", AsyncMock(return_value=s)),
        patch(f"{DB}.list_rules", AsyncMock(return_value=[_rule(2, 7, T(9, 5))])),
    ):
        await cog.ann_show.callback(cog, inter)
    text = _text(inter)
    assert "<#77>" in text
    assert "#2: 7 days before at 09:05" in text
    assert "General announcement: on" in text
    assert "Start announcement: off" in text
    assert "General template: default" in text
    assert "Start template: custom" in text


@pytest.mark.asyncio
async def test_show_without_channel_or_rules() -> None:
    cog = _cog(_ctx())
    inter = _inter()
    with (
        patch(f"{SET}.load_settings", AsyncMock(return_value=_settings())),
        patch(f"{DB}.list_rules", AsyncMock(return_value=[])),
    ):
        await cog.ann_show.callback(cog, inter)
    assert "not set" in _text(inter)


@pytest.mark.asyncio
async def test_general_on_creates_the_default_rule_and_says_so() -> None:
    cog = _cog(_ctx())
    inter = _inter()
    set_enabled = AsyncMock()
    with (
        patch(f"{SET}.load_settings", AsyncMock(return_value=_settings(channel_id=5))),
        patch(f"{SET}.set_enabled", set_enabled),
        patch(f"{DB}.list_rules", AsyncMock(side_effect=[[], [_rule()]])),
    ):
        await cog.ann_general.callback(cog, inter, "on")
    set_enabled.assert_awaited_once_with(cog.ctx.db, 111, GENERAL, True)
    text = _text(inter)
    assert "3 days before at 18:00" in text
    assert "nothing posts" not in text


@pytest.mark.asyncio
async def test_general_on_twice_does_not_move_the_watermark() -> None:
    cog = _cog(_ctx())
    inter = _inter()
    set_enabled = AsyncMock()
    with (
        patch(f"{SET}.load_settings", AsyncMock(return_value=_settings(general_enabled=True))),
        patch(f"{SET}.set_enabled", set_enabled),
    ):
        await cog.ann_general.callback(cog, inter, "on")
    set_enabled.assert_not_awaited()
    assert "already on" in _text(inter)


@pytest.mark.asyncio
async def test_off_when_already_off_says_so() -> None:
    cog = _cog(_ctx())
    inter = _inter()
    set_enabled = AsyncMock()
    with (
        patch(f"{SET}.load_settings", AsyncMock(return_value=_settings())),
        patch(f"{SET}.set_enabled", set_enabled),
    ):
        await cog.ann_start.callback(cog, inter, "off")
    set_enabled.assert_not_awaited()
    assert "already off" in _text(inter)


@pytest.mark.asyncio
async def test_start_on_without_channel_warns_that_nothing_posts_yet() -> None:
    cog = _cog(_ctx())
    inter = _inter()
    set_enabled = AsyncMock()
    with (
        patch(f"{SET}.load_settings", AsyncMock(return_value=_settings())),
        patch(f"{SET}.set_enabled", set_enabled),
    ):
        await cog.ann_start.callback(cog, inter, "on")
    set_enabled.assert_awaited_once_with(cog.ctx.db, 111, START, True)
    assert "nothing posts until a channel is set" in _text(inter).lower()


@pytest.mark.asyncio
async def test_channel_set_checks_permissions_and_clears() -> None:
    cog = _cog(_ctx())
    set_channel = AsyncMock()
    channel = MagicMock(id=42)
    channel.mention = "<#42>"
    ok = _inter()
    with (
        patch(f"{SET}.set_channel", set_channel),
        patch("bot.cogs.announcements_cog.missing_permissions", return_value=[]),
    ):
        await cog.ann_channel.callback(cog, ok, channel)
    set_channel.assert_awaited_once_with(cog.ctx.db, 111, 42)
    assert "<#42>" in _text(ok)

    set_channel.reset_mock()
    bad = _inter()
    with (
        patch(f"{SET}.set_channel", set_channel),
        patch(
            "bot.cogs.announcements_cog.missing_permissions", return_value=["Send Messages"]
        ),
    ):
        await cog.ann_channel.callback(cog, bad, channel)
    set_channel.assert_not_awaited()
    assert "Send Messages" in _text(bad)

    set_channel.reset_mock()
    clear = _inter()
    with patch(f"{SET}.set_channel", set_channel):
        await cog.ann_channel.callback(cog, clear, None)
    set_channel.assert_awaited_once_with(cog.ctx.db, 111, None)


@pytest.mark.asyncio
async def test_rule_add_edit_remove_validate_time_and_days() -> None:
    cog = _cog(_ctx())
    add = AsyncMock(return_value=_rule(5, 3, T(20, 0)))
    update = AsyncMock(return_value=_rule(5, 2, T(20, 0)))
    delete = AsyncMock(return_value=True)
    with (
        patch(f"{DB}.add_rule", add),
        patch(f"{DB}.update_rule", update),
        patch(f"{DB}.delete_rule", delete),
        patch(f"{DB}.list_rules", AsyncMock(return_value=[_rule(5, 3, T(20, 0))])),
    ):
        inter = _inter()
        await cog.rule_add.callback(cog, inter, 3, "20:00")
        add.assert_awaited_once_with(cog.ctx.db, 111, 3, T(20, 0))
        assert "#5: 3 days before at 20:00" in _text(inter)

        for bad_time in ("25:00", "abc", "18"):
            inter = _inter()
            await cog.rule_add.callback(cog, inter, 3, bad_time)
            assert "HH:MM" in _text(inter)
        for bad_days in (-1, 366):
            inter = _inter()
            await cog.rule_add.callback(cog, inter, bad_days, "18:00")
            assert "0 to 365" in _text(inter)
        assert add.await_count == 1

        inter = _inter()
        await cog.rule_edit.callback(cog, inter, 5, 2, None)
        update.assert_awaited_once_with(cog.ctx.db, 111, 5, 2, T(20, 0))
        assert "#5" in _text(inter)

        inter = _inter()
        await cog.rule_edit.callback(cog, inter, 5, None, None)
        assert "days or a time" in _text(inter)

        inter = _inter()
        await cog.rule_remove.callback(cog, inter, 5)
        delete.assert_awaited_once_with(cog.ctx.db, 111, 5)
        assert "removed" in _text(inter)


@pytest.mark.asyncio
async def test_rule_edit_and_remove_unknown_id() -> None:
    cog = _cog(_ctx())
    with (
        patch(f"{DB}.list_rules", AsyncMock(return_value=[])),
        patch(f"{DB}.delete_rule", AsyncMock(return_value=False)),
    ):
        inter = _inter()
        await cog.rule_edit.callback(cog, inter, 9, 2, None)
        assert "no rule" in _text(inter).lower()
        inter = _inter()
        await cog.rule_remove.callback(cog, inter, 9)
        assert "no rule" in _text(inter).lower()


@pytest.mark.asyncio
async def test_commands_are_ephemeral_and_refuse_in_maintenance() -> None:
    ctx = _ctx()
    cog = _cog(ctx)
    ctx.maintenance = "import"
    for name, call in _commands(cog).items():
        inter = _inter()
        await call(inter)
        assert inter.response.send_message.await_count == 1, name
        assert inter.response.send_message.call_args.kwargs["ephemeral"] is True, name


@pytest.mark.asyncio
async def test_not_ready_without_db() -> None:
    ctx = _ctx()
    ctx.db = None
    cog = _cog(ctx)
    inter = _inter()
    await cog.ann_show.callback(cog, inter)
    assert inter.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_loop_calls_tick() -> None:
    ctx = _ctx()
    cog = _cog(ctx)
    await cog._announce_cycle()
    ctx.announcement_service.tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_loop_skips_in_maintenance_or_without_service() -> None:
    ctx = _ctx()
    ctx.maintenance = "import"
    cog = _cog(ctx)
    await cog._announce_cycle()
    ctx.announcement_service.tick.assert_not_awaited()
    ctx.maintenance = None
    ctx.announcement_service = None
    await cog._announce_cycle()
