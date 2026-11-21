"""Tests for bot/web/schedule_page.py (parsing, messages, apply scope)."""
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.schedule import PendingChange, RuleConflict
from bot.services.schedule_rules import Rule
from bot.ui import texts
from bot.web.schedule_page import SchedulePageService
from tests.notion.factories import make_event

DAY = datetime.date(2026, 12, 5)
RULE = Rule(3, "location", "Hall B", 2, DAY)


def _page(**service_attrs):
    schedule = AsyncMock()
    for name, value in service_attrs.items():
        setattr(schedule, name, value)
    ctx = SimpleNamespace(schedule_service=schedule, db=object())
    bot = SimpleNamespace(guilds=[SimpleNamespace(id=7)])
    return SchedulePageService(ctx, bot), schedule


async def test_conflict_uses_the_discord_wording() -> None:
    page, schedule = _page()
    schedule.add_rule.side_effect = RuleConflict([RULE])
    schedule.upcoming_dates.return_value = []
    result = await page.add_rule("location", "Hall C", "2", "2026-12-05")
    assert not result.ok
    assert "collides with #3" in result.message and "Remove that rule first." in result.message


async def test_bad_input_is_refused_before_the_service() -> None:
    page, schedule = _page()
    assert (await page.add_rule("x", "v", "1", "2026-12-05")).message
    bad_every = await page.add_rule("genre", "v", "99x", "2026-12-05")
    assert bad_every.message == texts.RULE_EVERY_RANGE
    assert (
        await page.add_rule("genre", "v", "1", "soon")
    ).message == texts.RULE_ANCHOR_NOT_UPCOMING
    schedule.add_rule.assert_not_called()


async def test_value_errors_become_messages() -> None:
    page, schedule = _page()
    page.get_event = AsyncMock(return_value=make_event(page_id="u"))
    schedule.set_event_values.side_effect = ValueError(texts.GENRE_TOO_LONG)
    result = await page.set_event("u", "x" * 41, "", "")
    assert not result.ok and result.message == texts.GENRE_TOO_LONG


async def test_unchanged_form_fields_are_not_sent() -> None:
    from bot.services.schedule import UNSET

    page, schedule = _page()
    stored = make_event(page_id="u", genre="Techno", location="Club", notes="Old")
    page.get_event = AsyncMock(return_value=stored)
    schedule.set_event_values.return_value = stored
    assert (await page.set_event("u", " Techno ", "Club", "New")).ok
    schedule.set_event_values.assert_awaited_once_with(
        7, "u", genre=UNSET, location=UNSET, note="New"
    )


async def test_missing_event_is_refused_before_the_service() -> None:
    page, schedule = _page()
    page.get_event = AsyncMock(return_value=None)
    result = await page.set_event("u", "a", "b", "c")
    assert not result.ok
    schedule.set_event_values.assert_not_called()


async def test_apply_all_passes_only_the_shown_pairs() -> None:
    page, schedule = _page()
    schedule.apply.return_value = 1
    result = await page.apply("all", [], ["u1:genre", "bad", "u2:nope"])
    assert result.ok
    schedule.apply.assert_awaited_once_with(7, {("u1", "genre")})


async def test_apply_selected_without_picks_and_keep() -> None:
    page, schedule = _page()
    assert not (await page.apply("selected", [], ["u1:genre"])).ok
    keep = await page.apply("keep", [], ["u1:genre"])
    assert keep.ok and keep.message == texts.APPLY_KEPT
    schedule.apply.assert_not_called()


async def test_pending_uses_the_change_line() -> None:
    event = make_event(event_id="2026-12-05 Rave Night", page_id="u1", date=DAY)
    page, schedule = _page()
    schedule.pending_changes.return_value = [PendingChange(event, "genre", "A", "B")]
    views = await page.pending()
    assert views[0].uid == "u1" and views[0].text == texts.change_text(event, "genre", "A", "B")


async def test_no_guild_is_not_ready() -> None:
    ctx = SimpleNamespace(schedule_service=AsyncMock(), db=object())
    page = SchedulePageService(ctx, SimpleNamespace(guilds=[]))
    assert await page.overview() is None
    assert not (await page.set_defaults("a", "b")).ok
