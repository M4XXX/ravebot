"""Event announcements: posts and edits announcement messages in the announcement channel.

One `tick` handles every event. Each event yields "plans" (kind, rule id or None,
template): a plan without a post row is claimed, rendered and sent; a plan for an
existing `posted` row is re-rendered and the message edited when its digest changed.
The same claim/render/send/edit/mark code serves both announcement kinds.

General announcements (this module so far): every rule due for an upcoming event posts
once, but only when the lineup is fully booked; otherwise the rule is marked `gone` for
that event and never retried. Posted general announcements follow the event until it
starts. A canceled or deleted event gets its posted announcements edited once to a
fixed text. The type switch and the channel gate new posts only: existing posts are
kept current (edited, canceled) in their stored channel either way.

The start announcement (one post per event, no rule) is created while the event runs
(first slot start up to the end of the last slot) and only for a fully booked lineup;
an incomplete one is marked `gone`. It is edited on every tick when its digest changes
(slot change), once to the ended rendering at the event end (state `ended`, never touched
again), and canceled like the general posts. A bot that was offline posts it late.
A message deleted by hand (NotFound on edit) is marked `gone` and never reposted.
Ticks are serialised by a lock, so a row still `claimed` when a tick starts is stale
and released for a retry. Nothing here raises into the caller.
"""
import asyncio
import datetime
import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import disnake

from bot.db import announcements as ann_db
from bot.db import events as db_events
from bot.db.announcements import AnnouncementPost, AnnouncementRule
from bot.notion.models import FINISHED_STATUSES, STATUS_CANCELED, EventInfo
from bot.services import activation
from bot.services.announcement_settings import (
    AnnouncementSettings,
    is_rule_due,
    load_settings,
)
from bot.services.announcement_template import (
    GENERAL,
    MAX_LENGTH,
    START,
    check_template,
    event_end,
    render,
)
from bot.services.fully_booked import is_fully_booked
from bot.ui import texts

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

ALLOWED_MENTIONS = disnake.AllowedMentions(roles=True, users=False, everyone=False)

# Post states
CLAIMED = "claimed"
POSTED = "posted"
GONE = "gone"
CANCELED = "canceled"
ENDED = "ended"

# Report reasons
_MISSING = "missing"
_FORBIDDEN = "forbidden"
_TOO_LONG = "too_long"
_INVALID = "invalid"

# Result of _resolve_channel (local copy of the management lineup pattern)
_PRESENT = "present"
_CHANNEL_GONE = "gone"
_UNKNOWN = "unknown"

_TOO_LONG_PREFIX = "Too long"


@dataclass(frozen=True)
class Plan:
    kind: str
    rule_id: int | None
    template: str


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _template_hash(template: str) -> str:
    return digest(template)[:16]


class AnnouncementService:
    """Posts and edits event announcements."""

    def __init__(self, bot: Any, db: Any, log_service: "AdminLogService | None") -> None:
        self.bot = bot
        self.db = db
        self.log_service = log_service
        self._lock = asyncio.Lock()
        self._reported: set[tuple[Any, ...]] = set()

    def _get_guild_id(self) -> int | None:
        if not self.bot.guilds:
            logger.warning("AnnouncementService: no guilds, skipping")
            return None
        return self.bot.guilds[0].id

    # --- tick ---------------------------------------------------------------

    async def tick(self, now: datetime.datetime | None = None) -> None:
        """Post and edit due announcements; never raises."""
        try:
            async with self._lock:
                await self._tick(now or datetime.datetime.now(datetime.UTC))
        except Exception:
            logger.exception("AnnouncementService: tick failed")

    async def _tick(self, now: datetime.datetime) -> None:
        guild_id = self._get_guild_id()
        if guild_id is None or not await activation.is_active(self.db, guild_id):
            return
        posts = await ann_db.list_posts(self.db)
        for post in posts:
            if post.state == CLAIMED:  # ticks are serialised: a leftover claim is stale
                await ann_db.release_claim(self.db, post.id)
        settings = await load_settings(self.db, guild_id)
        # Existing posts are kept current (edited, canceled) in their stored channel
        # whatever the switches say; the switches and the channel gate new posts only.
        rules = await ann_db.list_rules(self.db, guild_id)
        all_events = await db_events.list_events(self.db, include_past=True)
        known = {e.page_id for e in all_events}
        events = await db_events.list_events(self.db)
        upcoming = {e.page_id for e in events}
        by_event: dict[str, list[AnnouncementPost]] = {}
        for post in posts:
            if post.state != CLAIMED:
                by_event.setdefault(post.event_id, []).append(post)
        for event_id, event_posts in by_event.items():
            if event_id in known:
                continue
            for post in event_posts:  # the event was deleted: like a cancel
                if post.state == POSTED:  # started events' general posts are `ended` already
                    try:
                        await self._cancel(post)
                    except Exception:
                        logger.exception("AnnouncementService: cancel of post %s failed", post.id)
        # A running event that left the upcoming list (past midnight) still needs its
        # start post created late or edited to the ended rendering.
        events = [*events, *(
            e for e in all_events
            if e.page_id not in upcoming
            and (now < event_end(e)
                 or any(p.kind == START and p.state == POSTED for p in by_event.get(e.page_id, [])))
        )]
        for event in events:
            try:
                await self._tick_event(
                    event, by_event.get(event.page_id, []), settings, rules, now
                )
            except Exception:
                logger.exception("AnnouncementService: event %s failed", event.page_id)

    async def _tick_event(
        self,
        event: EventInfo,
        posts: list[AnnouncementPost],
        settings: AnnouncementSettings,
        rules: list[AnnouncementRule],
        now: datetime.datetime,
    ) -> None:
        existing = {(p.kind, p.rule_id) for p in posts}
        live: list[AnnouncementPost] = []  # posted and still followed
        for post in posts:
            if post.state != POSTED:
                continue
            if self._editable(post, event, now):
                live.append(post)
            else:  # a general post is final once the event has started
                await ann_db.set_state(self.db, post.id, ENDED)
        if event.status == STATUS_CANCELED:
            for post in live:
                await self._cancel(post)
            return
        templates = self._templates(settings)
        if event.status not in FINISHED_STATUSES:
            for plan in self._new_plans(event, settings, rules, now):
                if settings.channel_id is not None and (plan.kind, plan.rule_id) not in existing:
                    await self._claim_and_post(event, plan, settings.channel_id, now)
        for post in live:
            if post.kind in templates:
                current = await self._update(
                    event, Plan(post.kind, post.rule_id, templates[post.kind]), post, now
                )
                if current and post.kind == START and now >= event_end(event):
                    await ann_db.set_state(self.db, post.id, ENDED)  # done for good

    # --- plans --------------------------------------------------------------

    @staticmethod
    def _templates(settings: AnnouncementSettings) -> dict[str, str]:
        """Template per kind whose posts are kept current."""
        return {GENERAL: settings.general_template, START: settings.start_template}

    @staticmethod
    def _new_plans(
        event: EventInfo,
        settings: AnnouncementSettings,
        rules: list[AnnouncementRule],
        now: datetime.datetime,
    ) -> list[Plan]:
        plans: list[Plan] = []
        if settings.general_enabled:
            plans += [
                Plan(GENERAL, rule.id, settings.general_template)
                for rule in rules
                if is_rule_due(event, rule, settings, now)
            ]
        if settings.start_enabled and event.slot_start(0) <= now < event_end(event):
            plans.append(Plan(START, None, settings.start_template))
        return plans

    @staticmethod
    def _editable(post: AnnouncementPost, event: EventInfo, now: datetime.datetime) -> bool:
        if post.kind == GENERAL:
            return now < event.slot_start(0)
        return post.kind == START

    # --- render -------------------------------------------------------------

    async def _render(self, event: EventInfo, plan: Plan, now: datetime.datetime) -> str | None:
        """The filled text, or None (reported once) when the template or length is bad."""
        check = check_template(plan.template, plan.kind, event)
        problems = [p for p in check.problems if not p.startswith(_TOO_LONG_PREFIX)]
        if problems:
            await self._report_template(
                (_INVALID, plan.kind, _template_hash(plan.template)),
                texts.announce_template_invalid(plan.kind, problems[0]),
            )
            return None
        text = render(plan.template, event, plan.kind, now)
        if len(text) > MAX_LENGTH:
            await self._report_template(
                (_TOO_LONG, plan.kind, _template_hash(plan.template), event.page_id),
                texts.announce_too_long(event.event_id, len(text), MAX_LENGTH),
            )
            return None
        return text

    # --- post ---------------------------------------------------------------

    async def _claim_and_post(
        self, event: EventInfo, plan: Plan, channel_id: int, now: datetime.datetime
    ) -> None:
        post = await ann_db.claim_post(self.db, event.page_id, plan.kind, plan.rule_id, channel_id)
        if post is None:
            return  # posted (or skipped) already
        if not is_fully_booked(event):
            await ann_db.set_state(self.db, post.id, GONE)  # skipped means skipped
            return
        message = None
        try:
            text = await self._render(event, plan, now)
            if text is None:
                await ann_db.release_claim(self.db, post.id)
                return
            message = await self._send_to(channel_id, text)
            if message is None:
                await ann_db.release_claim(self.db, post.id)
                return
            await ann_db.mark_posted(self.db, post.id, message.id, digest(text))
        except Exception:
            logger.exception(
                "AnnouncementService: posting %s/%s for %s failed",
                plan.kind, plan.rule_id, event.page_id,
            )
            if message is not None:  # sent but not recorded: undo, so a retry is no copy
                try:
                    await message.delete()
                except Exception:
                    logger.warning(
                        "AnnouncementService: could not delete unrecorded message %s",
                        message.id, exc_info=True,
                    )
            await ann_db.release_claim(self.db, post.id)

    async def _send_to(self, channel_id: int, text: str) -> Any:
        """The sent message, or None (reported once where it helps)."""
        state, channel = await self._resolve_channel(channel_id)
        if state == _CHANNEL_GONE:
            await self._report_channel(channel_id, _MISSING)
            return None
        if channel is None:
            return None
        try:
            return await self._send(channel, text)
        except disnake.Forbidden:
            await self._report_channel(channel_id, _FORBIDDEN)
        except Exception:
            logger.warning(
                "AnnouncementService: send to channel %s failed", channel_id, exc_info=True
            )
        return None

    async def _send(self, channel: Any, text: str, file: disnake.File | None = None) -> Any:
        """The one send path: roles may ping, users, @everyone and @here never."""
        kwargs: dict[str, Any] = {"content": text, "allowed_mentions": ALLOWED_MENTIONS}
        if file is not None:
            kwargs["file"] = file
        return await channel.send(**kwargs)

    # --- edit ---------------------------------------------------------------

    async def _update(
        self, event: EventInfo, plan: Plan, post: AnnouncementPost, now: datetime.datetime
    ) -> bool:
        """Edit the post when its text changed. True when the message is up to date."""
        text = await self._render(event, plan, now)
        if text is None:
            return False
        new_digest = digest(text)
        if new_digest == post.digest:
            return True
        if await self._edit(post, text):
            await ann_db.set_digest(self.db, post.id, new_digest)
            return True
        return False

    async def _cancel(self, post: AnnouncementPost) -> None:
        if await self._edit(post, texts.ANNOUNCE_CANCELED):
            await ann_db.set_state(self.db, post.id, CANCELED)

    async def _edit(self, post: AnnouncementPost, text: str) -> bool:
        """Edit the post's message in its own channel. True when edited.

        A message or channel that no longer exists marks the post `gone`; other
        failures keep it `posted` for the next tick.
        """
        if post.message_id is None:
            return False
        state, channel = await self._resolve_channel(post.channel_id)
        if state == _CHANNEL_GONE:
            await ann_db.set_state(self.db, post.id, GONE)
            return False
        if channel is None:
            return False
        try:
            await channel.get_partial_message(post.message_id).edit(
                content=text, allowed_mentions=ALLOWED_MENTIONS
            )
        except disnake.NotFound:
            await ann_db.set_state(self.db, post.id, GONE)  # deleted by hand
            return False
        except disnake.Forbidden:
            await self._report_channel(post.channel_id, _FORBIDDEN)
            return False
        except Exception:
            logger.warning(
                "AnnouncementService: edit of message %s failed", post.message_id, exc_info=True
            )
            return False
        return True

    # --- channel and reports (local copy of the management lineup helpers) -------

    async def _resolve_channel(self, channel_id: int) -> tuple[str, Any]:
        """(state, channel): present with the channel, gone once Discord says it is
        deleted, unknown (channel None) when that cannot be told yet."""
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return _PRESENT, channel
        if not self.bot.is_ready():
            return _UNKNOWN, None
        try:
            return _PRESENT, await self.bot.fetch_channel(channel_id)
        except disnake.NotFound:
            return _CHANNEL_GONE, None
        except Exception:
            logger.warning(
                "AnnouncementService: could not look up channel %s", channel_id, exc_info=True
            )
            return _UNKNOWN, None

    async def _report_channel(self, channel_id: int, reason: str) -> None:
        await self._report(
            ("channel", channel_id, reason),
            texts.announce_channel_problem(channel_id, reason),
            texts.ANNOUNCE_CHANNEL_TODO,
        )

    async def _report_template(self, key: tuple[Any, ...], what: str) -> None:
        await self._report(key, what, texts.ANNOUNCE_TEMPLATE_TODO)

    async def _report(self, key: tuple[Any, ...], what: str, todo: str) -> None:
        """One admin log entry per cause per process."""
        if key in self._reported:
            return
        self._reported.add(key)
        logger.warning("AnnouncementService: %s", what)
        if self.log_service is None:
            return
        try:
            await self.log_service.log_attention(texts.LOG_FAILED_TITLE, what, todo)
        except Exception:
            logger.exception("AnnouncementService: admin log entry failed")
