"""Tests for bot/services/announcements.py (general announcements)."""
import asyncio
import dataclasses
import datetime
import itertools
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from bot.db.announcements import AnnouncementPost, AnnouncementRule
from bot.services.announcement_settings import AnnouncementSettings
from bot.services.announcement_template import DEFAULT_TEMPLATES, GENERAL, MAX_LENGTH
from bot.services.announcements import AnnouncementService
from bot.timezone import get_timezone
from bot.ui import texts
from tests.fake_events import fake_events
from tests.notion.factories import make_event, make_slot

GUILD = 4242
CHANNEL = 500
OTHER_CHANNEL = 600
ZONE = get_timezone()
EVENT_DATE = datetime.date(2026, 12, 5)
CREATED = datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC)
# Rule "3 days before at 18:00" for EVENT_DATE posts at 2026-12-02 18:00 local
POST_AT = datetime.datetime(2026, 12, 2, 18, 0, tzinfo=ZONE)
DUE = POST_AT + datetime.timedelta(minutes=5)
START = datetime.datetime(2026, 12, 5, 20, 0, tzinfo=ZONE)

_ids = itertools.count(1000)


def _not_found() -> disnake.NotFound:
    return disnake.NotFound(MagicMock(status=404, reason="gone"), "Unknown Message")


def _forbidden() -> disnake.Forbidden:
    return disnake.Forbidden(MagicMock(status=403, reason="x"), "Missing Access")


class FakeMessage:
    def __init__(self, channel: "FakeChannel", kwargs: dict) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.kwargs = kwargs
        self.content = kwargs.get("content")

    async def edit(self, **kwargs) -> None:
        if self.id in self.channel.deleted:
            raise _not_found()
        if self.channel.edit_error is not None:
            raise self.channel.edit_error
        self.content = kwargs.get("content")
        self.channel.edit_calls.append((self.id, kwargs))


class _GoneMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id

    async def edit(self, **kwargs) -> None:
        raise _not_found()


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []
        self.edit_calls: list[tuple[int, dict]] = []
        self.deleted: set[int] = set()
        self.send_error: Exception | None = None
        self.edit_error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def send(self, **kwargs) -> FakeMessage:
        await asyncio.sleep(0)
        if self.gate is not None:
            await self.gate.wait()
        if self.send_error is not None:
            raise self.send_error
        msg = FakeMessage(self, kwargs)
        self.messages[msg.id] = msg
        self.sent.append(msg)
        return msg

    def get_partial_message(self, message_id: int):
        return self.messages.get(message_id) or _GoneMessage(message_id)


class FakeStore:
    """In-memory announcement_rules and announcement_posts tables."""

    def __init__(self) -> None:
        self.rules: list[AnnouncementRule] = []
        self.posts: dict[int, AnnouncementPost] = {}
        self._next = itertools.count(1)
        self.claims = 0

    def add_rule(self, days: int = 3, at: datetime.time = datetime.time(18, 0),
                 created: datetime.datetime = CREATED) -> AnnouncementRule:
        rule = AnnouncementRule(len(self.rules) + 1, days, at, created)
        self.rules.append(rule)
        return rule

    def _update(self, post_id: int, **changes) -> None:
        self.posts[post_id] = dataclasses.replace(self.posts[post_id], **changes)

    def patch(self, monkeypatch) -> None:
        async def list_rules(db, guild_id):
            assert guild_id == GUILD
            return list(self.rules)

        async def claim_post(db, event_id, kind, rule_id, channel_id):
            await asyncio.sleep(0)
            self.claims += 1
            for p in self.posts.values():
                if (p.event_id, p.kind, p.rule_id) == (event_id, kind, rule_id):
                    return None
            post = AnnouncementPost(
                next(self._next), event_id, kind, rule_id, channel_id, None, "claimed", ""
            )
            self.posts[post.id] = post
            return post

        async def get_post(db, event_id, kind, rule_id):
            for p in self.posts.values():
                if (p.event_id, p.kind, p.rule_id) == (event_id, kind, rule_id):
                    return p
            return None

        async def list_posts(db, event_id=None):
            return [p for p in self.posts.values() if event_id in (None, p.event_id)]

        async def mark_posted(db, post_id, message_id, digest):
            self._update(post_id, state="posted", message_id=message_id, digest=digest)

        async def set_digest(db, post_id, digest):
            self._update(post_id, digest=digest)

        async def set_state(db, post_id, state):
            self._update(post_id, state=state)

        async def release_claim(db, post_id):
            if post_id in self.posts and self.posts[post_id].state == "claimed":
                del self.posts[post_id]

        base = "bot.services.announcements.ann_db"
        for fn in (list_rules, claim_post, get_post, list_posts, mark_posted, set_digest,
                   set_state, release_claim):
            monkeypatch.setattr(f"{base}.{fn.__name__}", fn)

    def by_state(self, state: str) -> list[AnnouncementPost]:
        return [p for p in self.posts.values() if p.state == state]


class Env:
    def __init__(self, monkeypatch) -> None:
        self.store = FakeStore()
        self.store.patch(monkeypatch)
        self.active = True
        self.settings = AnnouncementSettings(
            channel_id=CHANNEL,
            general_enabled=True,
            start_enabled=False,
            general_template=DEFAULT_TEMPLATES[GENERAL],
            start_template="",
            general_since=CREATED,
        )
        self.channels = {CHANNEL: FakeChannel(CHANNEL), OTHER_CHANNEL: FakeChannel(OTHER_CHANNEL)}

        async def is_active(db, guild_id):
            assert guild_id == GUILD
            return self.active

        async def load_settings(db, guild_id):
            assert guild_id == GUILD
            return self.settings

        monkeypatch.setattr("bot.services.announcements.activation.is_active", is_active)
        monkeypatch.setattr("bot.services.announcements.load_settings", load_settings)

        self.bot = MagicMock()
        self.bot.guilds = [MagicMock(id=GUILD)]
        self.bot.get_channel = MagicMock(side_effect=lambda cid: self.channels.get(cid))
        self.bot.is_ready = MagicMock(return_value=True)
        self.bot.fetch_channel = AsyncMock(side_effect=_not_found())
        self.log = MagicMock(log_attention=AsyncMock())
        self.service = AnnouncementService(self.bot, MagicMock(), self.log)

    @property
    def channel(self) -> FakeChannel:
        return self.channels[CHANNEL]

    def events(self, *events) -> None:
        fake_events.list_events = AsyncMock(return_value=list(events))

    def configure(self, **changes) -> None:
        self.settings = dataclasses.replace(self.settings, **changes)


@pytest.fixture
def env(monkeypatch) -> Env:
    return Env(monkeypatch)


def _event(page_id: str = "ev-1", booked: int = 3, count: int = 3, status: str | None = "Planned",
           names: tuple[str, ...] = (), **kw):
    slots = [
        make_slot(i, dj_page_id=f"dj-{i}",
                  dj_name=names[i - 1] if i <= len(names) else f"DJ {i}", discord_id=100 + i)
        if i <= booked
        else make_slot(i)
        for i in range(1, count + 1)
    ]
    return make_event(
        event_id=f"Event {page_id}", page_id=page_id, date=EVENT_DATE, status=status,
        slot_count=count, slots=slots, **kw,
    )


# --- posting ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_posts_once_per_event_and_rule(env: Env) -> None:
    r1 = env.store.add_rule(3)
    r2 = env.store.add_rule(5)
    env.events(_event("ev-1"), _event("ev-2"))
    await env.service.tick(DUE)
    await env.service.tick(DUE + datetime.timedelta(hours=1))
    # Both rules are due (5 days before passed too), one message per event and rule
    assert len(env.channel.sent) == 4
    keys = sorted((p.event_id, p.rule_id) for p in env.store.by_state("posted"))
    assert keys == [("ev-1", r1.id), ("ev-1", r2.id), ("ev-2", r1.id), ("ev-2", r2.id)]
    post = env.store.by_state("posted")[0]
    assert post.channel_id == CHANNEL
    assert post.message_id in env.channel.messages
    assert post.digest
    assert "Event ev-1" in env.channel.sent[0].content


@pytest.mark.asyncio
async def test_two_concurrent_ticks_post_once(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    await asyncio.gather(env.service.tick(DUE), env.service.tick(DUE))
    assert len(env.channel.sent) == 1
    assert len(env.store.posts) == 1


@pytest.mark.asyncio
async def test_tick_overlapping_a_running_send_waits_and_posts_once(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    env.channel.gate = asyncio.Event()
    first = asyncio.create_task(env.service.tick(DUE))
    for _ in range(20):
        await asyncio.sleep(0)
    second = asyncio.create_task(env.service.tick(DUE))
    for _ in range(20):
        await asyncio.sleep(0)
    env.channel.gate.set()
    await asyncio.gather(first, second)
    assert len(env.channel.sent) == 1
    assert [p.state for p in env.store.posts.values()] == ["posted"]


@pytest.mark.asyncio
async def test_incomplete_lineup_is_skipped_and_not_retried_later(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event(booked=2))
    await env.service.tick(DUE)
    assert env.channel.sent == []
    assert [p.state for p in env.store.posts.values()] == ["gone"]
    env.events(_event(booked=3))
    await env.service.tick(DUE + datetime.timedelta(hours=1))
    assert env.channel.sent == []


@pytest.mark.asyncio
async def test_rule_not_due_before_its_time_and_not_after_the_start(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    await env.service.tick(POST_AT - datetime.timedelta(minutes=1))
    assert env.channel.sent == []
    await env.service.tick(START)
    assert env.channel.sent == []
    assert env.store.posts == {}
    # A rule created after its post time does not post late either
    env.store.rules = []
    env.store.add_rule(3, created=DUE)
    await env.service.tick(DUE + datetime.timedelta(minutes=1))
    assert env.channel.sent == []


@pytest.mark.asyncio
async def test_no_rules_means_no_post(env: Env) -> None:
    env.events(_event())
    await env.service.tick(DUE)
    assert env.channel.sent == []
    assert env.store.posts == {}


@pytest.mark.asyncio
async def test_disabled_or_no_channel_posts_nothing(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    env.configure(general_enabled=False)
    await env.service.tick(DUE)
    env.configure(general_enabled=True, channel_id=None)
    await env.service.tick(DUE)
    assert env.channel.sent == []
    assert env.store.posts == {}


@pytest.mark.asyncio
async def test_idle_bot_posts_nothing(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    env.active = False
    await env.service.tick(DUE)
    assert env.channel.sent == []
    env.bot.guilds = []
    env.active = True
    await env.service.tick(DUE)
    assert env.channel.sent == []


# --- editing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_when_lineup_changes_until_the_start_only(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    await env.service.tick(DUE)
    msg = env.channel.sent[0]
    # Unchanged: no edit
    await env.service.tick(DUE + datetime.timedelta(minutes=1))
    assert env.channel.edit_calls == []
    # Changed lineup before the start: one edit, digest updated
    env.events(_event(names=("Alpha", "Beta", "Gamma")))
    await env.service.tick(DUE + datetime.timedelta(minutes=2))
    assert len(env.channel.edit_calls) == 1
    assert "Alpha" in msg.content
    edit_kwargs = env.channel.edit_calls[0][1]
    assert edit_kwargs["allowed_mentions"].everyone is False
    # A lineup that becomes incomplete still edits the post (it is not removed)
    env.events(_event(booked=2, names=("Alpha", "Beta")))
    await env.service.tick(DUE + datetime.timedelta(minutes=3))
    assert len(env.channel.edit_calls) == 2
    assert [p.state for p in env.store.posts.values()] == ["posted"]
    # After the start: no more edits
    env.events(_event(names=("Omega",)))
    await env.service.tick(START)
    assert len(env.channel.edit_calls) == 2
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_canceled_event_gets_the_fixed_text_once(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    await env.service.tick(DUE)
    env.events(_event(status="Canceled"))
    await env.service.tick(DUE + datetime.timedelta(minutes=1))
    await env.service.tick(DUE + datetime.timedelta(minutes=2))
    assert len(env.channel.edit_calls) == 1
    assert env.channel.sent[0].content == texts.ANNOUNCE_CANCELED
    assert [p.state for p in env.store.posts.values()] == ["canceled"]
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_canceled_event_gets_no_new_post(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event(status="Canceled"), _event("ev-2", status="Done"))
    await env.service.tick(DUE)
    assert env.channel.sent == []
    assert env.store.posts == {}


@pytest.mark.asyncio
async def test_deleted_message_is_not_reposted(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    await env.service.tick(DUE)
    env.channel.deleted.add(env.channel.sent[0].id)
    env.events(_event(names=("Alpha",)))
    await env.service.tick(DUE + datetime.timedelta(minutes=1))
    await env.service.tick(DUE + datetime.timedelta(minutes=2))
    assert len(env.channel.sent) == 1
    assert [p.state for p in env.store.posts.values()] == ["gone"]


@pytest.mark.asyncio
async def test_forbidden_edit_keeps_post_and_reports_once(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    await env.service.tick(DUE)
    env.channel.edit_error = _forbidden()
    env.events(_event(names=("Alpha",)))
    await env.service.tick(DUE + datetime.timedelta(minutes=1))
    await env.service.tick(DUE + datetime.timedelta(minutes=2))
    assert [p.state for p in env.store.posts.values()] == ["posted"]
    assert env.log.log_attention.await_count == 1
    # Fixed permission: the edit goes through on the next tick
    env.channel.edit_error = None
    await env.service.tick(DUE + datetime.timedelta(minutes=3))
    assert len(env.channel.edit_calls) == 1


@pytest.mark.asyncio
async def test_channel_change_keeps_editing_old_posts_in_their_channel(env: Env) -> None:
    r1 = env.store.add_rule(5)
    env.events(_event())
    await env.service.tick(datetime.datetime(2026, 11, 30, 18, 5, tzinfo=ZONE))
    assert len(env.channel.sent) == 1
    env.configure(channel_id=OTHER_CHANNEL)
    env.store.add_rule(3)
    env.events(_event(names=("Alpha",)))
    await env.service.tick(DUE)
    other = env.channels[OTHER_CHANNEL]
    assert len(other.sent) == 1
    assert len(env.channel.sent) == 1
    assert len(env.channel.edit_calls) == 1
    assert other.edit_calls == []
    old = next(p for p in env.store.posts.values() if p.rule_id == r1.id)
    assert old.channel_id == CHANNEL


# --- failures --------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_long_fill_sends_nothing_and_logs_once(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event(notes="x" * (MAX_LENGTH + 10)))
    await env.service.tick(DUE)
    await env.service.tick(DUE + datetime.timedelta(minutes=1))
    assert env.channel.sent == []
    assert env.store.posts == {}
    assert env.log.log_attention.await_count == 1
    what = env.log.log_attention.await_args.args[1]
    assert "Event ev-1" in what
    # Shorter notes: the next tick posts
    env.events(_event(notes="short"))
    await env.service.tick(DUE + datetime.timedelta(minutes=2))
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_invalid_template_sends_nothing_and_logs_once(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event(), _event("ev-2"))
    env.configure(general_template="**{title}** {oops")
    await env.service.tick(DUE)
    await env.service.tick(DUE + datetime.timedelta(minutes=1))
    assert env.channel.sent == []
    assert env.store.posts == {}
    assert env.log.log_attention.await_count == 1
    # A new template version is reported again
    env.configure(general_template="**{title}** {oops2")
    await env.service.tick(DUE + datetime.timedelta(minutes=2))
    assert env.log.log_attention.await_count == 2


@pytest.mark.asyncio
async def test_failed_send_leaves_no_claim(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    env.channel.send_error = RuntimeError("boom")
    await env.service.tick(DUE)
    assert env.store.posts == {}
    env.channel.send_error = _forbidden()
    await env.service.tick(DUE)
    await env.service.tick(DUE)
    assert env.store.posts == {}
    assert env.log.log_attention.await_count == 1
    env.channel.send_error = None
    await env.service.tick(DUE)
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_missing_channel_posts_nothing_and_reports_once(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    env.configure(channel_id=999)
    await env.service.tick(DUE)
    await env.service.tick(DUE)
    assert env.store.posts == {}
    assert env.log.log_attention.await_count == 1


@pytest.mark.asyncio
async def test_stale_claim_is_released_and_retried(env: Env) -> None:
    rule = env.store.add_rule(3)
    env.events(_event())
    post = AnnouncementPost(99, "ev-1", GENERAL, rule.id, CHANNEL, None, "claimed", "")
    env.store.posts[99] = post
    await env.service.tick(DUE)
    assert 99 not in env.store.posts
    assert len(env.channel.sent) == 1


@pytest.mark.asyncio
async def test_tick_never_raises(env: Env) -> None:
    env.store.add_rule(3)
    fake_events.list_events = AsyncMock(side_effect=RuntimeError("db down"))
    await env.service.tick(DUE)


# --- sending ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_roles_only_allowed_mentions(env: Env) -> None:
    env.store.add_rule(3)
    env.events(_event())
    env.configure(general_template="<@&123> @everyone **{title}**")
    await env.service.tick(DUE)
    mentions = env.channel.sent[0].kwargs["allowed_mentions"]
    assert mentions.roles is True
    assert mentions.users is False
    assert mentions.everyone is False


@pytest.mark.asyncio
async def test_send_helper_accepts_an_optional_file(env: Env) -> None:
    channel = env.channel
    await env.service._send(channel, "hello")
    assert "file" not in channel.sent[0].kwargs
    file = MagicMock(spec=disnake.File)
    await env.service._send(channel, "with poster", file=file)
    assert channel.sent[1].kwargs["file"] is file
    assert channel.sent[1].kwargs["content"] == "with poster"
