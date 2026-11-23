"""Tests for the event start announcement in bot/services/announcements.py."""
import datetime

import pytest

from bot.services.announcement_template import DEFAULT_TEMPLATES, START
from bot.ui import texts
from tests.services.test_announcements import (
    DUE,
    Env,
    _event,
)
from tests.services.test_announcements import (
    START as EVENT_START,
)

MIN = datetime.timedelta(minutes=1)
HOUR = datetime.timedelta(hours=1)
# _event: 3 slots of 60 minutes from 20:00
END = EVENT_START + 3 * HOUR


@pytest.fixture
def env(monkeypatch) -> Env:
    result = Env(monkeypatch)
    result.configure(general_enabled=False, start_enabled=True,
                     start_template=DEFAULT_TEMPLATES[START])
    return result


@pytest.mark.asyncio
async def test_posted_at_the_start_not_before(env: Env) -> None:
    env.events(_event(names=("Ann", "Bob", "Cy")))
    await env.service.tick(EVENT_START - MIN)
    assert env.channel.sent == []
    await env.service.tick(EVENT_START)
    assert len(env.channel.sent) == 1
    post = env.store.by_state("posted")[0]
    assert (post.kind, post.rule_id, post.channel_id) == ("start", None, 500)
    assert "Now playing: Ann" in env.channel.sent[0].content
    assert "Up next: Bob" in env.channel.sent[0].content


@pytest.mark.asyncio
async def test_edited_at_each_slot_change_with_now_playing_and_next_up(env: Env) -> None:
    env.events(_event(names=("Ann", "Bob", "Cy")))
    await env.service.tick(EVENT_START)
    await env.service.tick(EVENT_START + 30 * MIN)
    assert env.channel.edit_calls == []
    await env.service.tick(EVENT_START + HOUR)
    assert len(env.channel.edit_calls) == 1
    content = env.channel.sent[0].content
    assert "Now playing: Bob" in content
    assert "Up next: Cy" in content
    await env.service.tick(EVENT_START + 2 * HOUR)
    assert len(env.channel.edit_calls) == 2
    assert "Now playing: Cy" in env.channel.sent[0].content
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_last_slot_end_edits_once_to_ended_and_stops(env: Env) -> None:
    env.configure(start_template="{title}\n{status}")
    env.events(_event())
    await env.service.tick(EVENT_START)
    await env.service.tick(END)
    assert "The event has ended." in env.channel.sent[0].content
    assert [p.state for p in env.store.posts.values()] == ["ended"]
    edits = len(env.channel.edit_calls)
    await env.service.tick(END + HOUR)
    await env.service.tick(END + 5 * HOUR)
    assert len(env.channel.edit_calls) == edits
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_ended_edit_still_happens_after_the_event_left_the_upcoming_list(
    env: Env,
) -> None:
    env.configure(start_template="{title}\n{status}")
    ev = _event()
    env.events(ev)
    await env.service.tick(EVENT_START)
    env.events()  # a past-midnight tick: no longer upcoming
    env.past = [ev]
    await env.service.tick(END)
    assert "The event has ended." in env.channel.sent[0].content
    assert [p.state for p in env.store.posts.values()] == ["ended"]


@pytest.mark.asyncio
async def test_restart_does_not_post_twice(env: Env) -> None:
    env.events(_event())
    await env.service.tick(EVENT_START)
    fresh = type(env.service)(env.bot, env.service.db, env.log)
    await fresh.tick(EVENT_START + 10 * MIN)
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_late_start_after_downtime_posts_until_the_end(env: Env) -> None:
    env.events(_event(names=("Ann", "Bob", "Cy")))
    await env.service.tick(EVENT_START + 90 * MIN)
    assert len(env.channel.sent) == 1
    assert "Now playing: Bob" in env.channel.sent[0].content


@pytest.mark.asyncio
async def test_nothing_posted_after_the_event_ended(env: Env) -> None:
    env.events(_event())
    await env.service.tick(END)
    await env.service.tick(END + HOUR)
    assert env.channel.sent == []
    assert env.store.posts == {}


@pytest.mark.asyncio
async def test_incomplete_lineup_at_start_means_no_start_post(env: Env) -> None:
    env.events(_event(booked=2))
    await env.service.tick(EVENT_START)
    assert env.channel.sent == []
    assert [p.state for p in env.store.posts.values()] == ["gone"]
    env.events(_event(booked=3))  # filled later: still never posted
    await env.service.tick(EVENT_START + 10 * MIN)
    assert env.channel.sent == []


@pytest.mark.asyncio
async def test_start_time_moved_before_the_post_uses_the_new_time(env: Env) -> None:
    env.events(_event())
    await env.service.tick(EVENT_START - 30 * MIN)
    assert env.channel.sent == []
    env.events(_event(start=datetime.time(19, 0)))
    await env.service.tick(EVENT_START - 30 * MIN)
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_start_time_moved_after_the_post_keeps_editing_by_new_slot_times(
    env: Env,
) -> None:
    env.events(_event(names=("Ann", "Bob", "Cy")))
    await env.service.tick(EVENT_START)
    env.events(_event(names=("Ann", "Bob", "Cy"), start=datetime.time(19, 0)))
    await env.service.tick(EVENT_START)  # now slot 2 under the new times
    assert len(env.channel.sent) == 1
    assert "Now playing: Bob" in env.channel.sent[0].content


@pytest.mark.asyncio
async def test_start_and_general_posts_are_independent(env: Env) -> None:
    env.configure(general_enabled=True)
    env.store.add_rule(3)
    env.events(_event())
    await env.service.tick(DUE)
    assert len(env.channel.sent) == 1  # general only
    await env.service.tick(EVENT_START)
    assert sorted(p.kind for p in env.store.by_state("posted")) == ["general", "start"]
    assert len(env.channel.sent) == 2


@pytest.mark.asyncio
async def test_switch_off_or_channel_cleared_stops_only_new_posts(env: Env) -> None:
    env.events(_event(names=("Ann", "Bob", "Cy")))
    await env.service.tick(EVENT_START)
    env.configure(start_enabled=False, channel_id=None)
    await env.service.tick(EVENT_START + HOUR)
    assert "Now playing: Bob" in env.channel.sent[0].content
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_switch_off_posts_no_new_start(env: Env) -> None:
    env.configure(start_enabled=False)
    env.events(_event())
    await env.service.tick(EVENT_START)
    assert env.channel.sent == []


@pytest.mark.asyncio
async def test_canceled_event_gets_the_canceled_text(env: Env) -> None:
    env.events(_event())
    await env.service.tick(EVENT_START)
    env.events(_event(status="Canceled"))
    await env.service.tick(EVENT_START + MIN)
    assert env.channel.sent[0].content == texts.ANNOUNCE_CANCELED
    assert [p.state for p in env.store.posts.values()] == ["canceled"]


@pytest.mark.asyncio
async def test_canceled_event_gets_no_start_post(env: Env) -> None:
    env.events(_event(status="Canceled"))
    await env.service.tick(EVENT_START)
    assert env.channel.sent == []
