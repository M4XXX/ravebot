"""Tests for bot/cogs/schedule_cog.py (/schedule commands)."""
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.services.schedule import PendingChange, RuleConflict, ScheduleDate
from bot.services.schedule_rules import Rule
from bot.ui import texts
from bot.ui.schedule_views import ApplyChangesView
from tests.notion.factories import make_event

DAY = datetime.date(2026, 12, 5)


def _ctx(admins=frozenset({1})) -> MagicMock:
    ctx = MagicMock()
    ctx.is_admin = lambda uid: uid in admins
    ctx.maintenance = None
    ctx.db = MagicMock()
    service = MagicMock()
    service.pending_changes = AsyncMock(return_value=[])
    service.upcoming_dates = AsyncMock(
        return_value=[ScheduleDate(DAY, make_event(event_id=f"{DAY} Rave Night", date=DAY))]
    )
    service.add_rule = AsyncMock(
        side_effect=lambda g, r: Rule(4, r.field, r.value, r.every, r.anchor)
    )
    service.edit_rule = AsyncMock(side_effect=lambda g, r: r)
    service.remove_rule = AsyncMock(return_value=True)
    service.set_default = AsyncMock()
    ctx.schedule_service = service
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
    from bot.cogs.schedule_cog import ScheduleCog

    return ScheduleCog(ctx)


def _reply(inter) -> tuple[str, dict]:
    args, kwargs = inter.followup.send.call_args
    assert kwargs["ephemeral"] is True
    return args[0], kwargs


def _pending() -> list[PendingChange]:
    return [PendingChange(make_event(page_id="u", event_id=f"{DAY} Rave Night", date=DAY),
                          "genre", None, "Techno")]


@pytest.mark.asyncio
async def test_non_admin_refused() -> None:
    cog = _cog(_ctx())
    assert await cog.cog_slash_command_check(_inter(uid=99)) is False


@pytest.mark.asyncio
async def test_rule_add_without_pending_says_no_changes() -> None:
    ctx = _ctx()
    cog = _cog(ctx)
    inter = _inter()
    await cog.rule_add.callback(cog, inter, "location", "B", 2, DAY.isoformat())
    text, kwargs = _reply(inter)
    assert 'Rule added: #4 · Location "B" · every 2 · from Rave Night' in text
    assert texts.SCHEDULE_NO_CHANGES in text
    assert "view" not in kwargs


@pytest.mark.asyncio
async def test_rule_add_offers_apply_view_when_pending() -> None:
    ctx = _ctx()
    ctx.schedule_service.pending_changes.return_value = _pending()
    cog = _cog(ctx)
    inter = _inter()
    await cog.rule_add.callback(cog, inter, "genre", "Techno", 1, DAY.isoformat())
    text, kwargs = _reply(inter)
    assert isinstance(kwargs["view"], ApplyChangesView)
    assert "Rave Night · 05.12. · \U0001f3b5 none → Techno" in text


@pytest.mark.asyncio
async def test_rule_add_conflict_message_names_rule() -> None:
    ctx = _ctx()
    other = Rule(3, "location", "B", 2, datetime.date(2026, 12, 12))
    ctx.schedule_service.add_rule.side_effect = RuleConflict([other])
    cog = _cog(ctx)
    inter = _inter()
    await cog.rule_add.callback(cog, inter, "location", "C", 2, DAY.isoformat())
    text, _ = _reply(inter)
    assert text == (
        'This rule collides with #3 (Location "B", every 2, from Rave Night · 12.12.2026). '
        "Remove that rule first."
    )


@pytest.mark.asyncio
async def test_rule_add_error_text_is_shown() -> None:
    ctx = _ctx()
    ctx.schedule_service.add_rule.side_effect = ValueError(texts.RULE_VALUE_EMPTY)
    cog = _cog(ctx)
    inter = _inter()
    await cog.rule_add.callback(cog, inter, "genre", " ", 1, DAY.isoformat())
    assert _reply(inter)[0] == texts.RULE_VALUE_EMPTY


@pytest.mark.asyncio
async def test_default_without_value_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bot.cogs.schedule_cog.db_rules.get_default", AsyncMock(return_value=None))
    ctx = _ctx()
    cog = _cog(ctx)
    inter = _inter()
    await cog.schedule_default.callback(cog, inter, "genre", None)
    ctx.schedule_service.set_default.assert_awaited_once_with(111, "genre", None)
    assert texts.default_set("genre", None) in _reply(inter)[0]


@pytest.mark.asyncio
async def test_rule_edit_needs_an_option() -> None:
    cog = _cog(_ctx())
    inter = _inter()
    await cog.rule_edit.callback(cog, inter, "3", None, None, None)
    assert inter.response.send_message.call_args.args[0] == texts.RULE_EDIT_NEEDS_OPTION


@pytest.mark.asyncio
async def test_rule_edit_keeps_unchanged_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = Rule(3, "location", "B", 2, DAY)
    monkeypatch.setattr("bot.cogs.schedule_cog.db_rules.get_rule", AsyncMock(return_value=stored))
    ctx = _ctx()
    cog = _cog(ctx)
    inter = _inter()
    await cog.rule_edit.callback(cog, inter, "3", None, 4, None)
    ctx.schedule_service.edit_rule.assert_awaited_once_with(
        111, Rule(3, "location", "B", 4, DAY)
    )
    assert "Rule updated: #3" in _reply(inter)[0]


@pytest.mark.asyncio
async def test_rule_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = Rule(3, "location", "B", 2, DAY)
    monkeypatch.setattr("bot.cogs.schedule_cog.db_rules.get_rule", AsyncMock(return_value=stored))
    ctx = _ctx()
    cog = _cog(ctx)
    inter = _inter()
    await cog.rule_remove.callback(cog, inter, "3")
    ctx.schedule_service.remove_rule.assert_awaited_once_with(111, 3)
    assert texts.rule_removed(3) in _reply(inter)[0]


@pytest.mark.asyncio
async def test_rule_remove_unknown_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bot.cogs.schedule_cog.db_rules.get_rule", AsyncMock(return_value=None))
    cog = _cog(_ctx())
    inter = _inter()
    await cog.rule_remove.callback(cog, inter, "9")
    assert _reply(inter)[0] == texts.RULE_NOT_FOUND


@pytest.mark.asyncio
async def test_schedule_apply_opens_view() -> None:
    ctx = _ctx()
    ctx.schedule_service.pending_changes.return_value = _pending()
    cog = _cog(ctx)
    inter = _inter()
    await cog.schedule_apply.callback(cog, inter)
    assert isinstance(_reply(inter)[1]["view"], ApplyChangesView)


@pytest.mark.asyncio
async def test_schedule_show_marks_hand_values(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    event = dataclasses.replace(
        make_event(event_id=f"{DAY} Rave Night", date=DAY, genre="Techno", location="B"),
        genre_by_hand=True,
    )
    ctx = _ctx()
    ctx.schedule_service.upcoming_dates.return_value = [ScheduleDate(DAY, event)]
    monkeypatch.setattr(
        "bot.cogs.schedule_cog.db_rules.get_default",
        AsyncMock(side_effect=lambda db, g, f: "Mixed" if f == "genre" else None),
    )
    monkeypatch.setattr(
        "bot.cogs.schedule_cog.db_rules.list_rules",
        AsyncMock(return_value=[Rule(3, "location", "B", 2, DAY)]),
    )
    cog = _cog(ctx)
    inter = _inter()
    await cog.schedule_show.callback(cog, inter)
    text, _ = _reply(inter)
    assert 'Genre: "Mixed"' in text
    assert '#3 · Location "B" · every 2 · from Rave Night · 05.12.2026' in text
    assert "\U0001f3b5 Techno (set by hand) · \U0001f4cd B" in text


@pytest.mark.asyncio
async def test_start_autocomplete_returns_label_to_iso_max_25(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=True))
    ctx = _ctx()
    dates = [
        ScheduleDate(DAY + datetime.timedelta(days=7 * i), None) for i in range(30)
    ]
    ctx.schedule_service.upcoming_dates.return_value = dates
    cog = _cog(ctx)
    out = await cog.rule_start_autocomplete(_inter(), "")
    assert ctx.schedule_service.upcoming_dates.call_args.args == (111, 25)
    assert out["Rave Night · 05.12.2026"] == "2026-12-05"
    assert await cog.rule_start_autocomplete(_inter(uid=99), "") == {}


@pytest.mark.asyncio
async def test_autocomplete_nothing_while_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=False))
    cog = _cog(_ctx())
    assert await cog.rule_start_autocomplete(_inter(), "") == {}


def test_command_tree() -> None:
    names = set()
    for cmd in _cog(_ctx()).get_slash_commands():
        for child in cmd.children.values():
            grand = getattr(child, "children", None) or {}
            for g in grand.values():
                names.add(g.qualified_name)
            if not grand:
                names.add(child.qualified_name)
    assert names == {
        "schedule show", "schedule default", "schedule apply",
        "schedule rule add", "schedule rule edit", "schedule rule remove",
    }
