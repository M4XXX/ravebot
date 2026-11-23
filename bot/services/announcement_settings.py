"""Event announcement settings (guild config) and the due-window logic."""
import datetime
from dataclasses import dataclass

import asyncpg

from bot.db import announcements, config
from bot.db.announcements import AnnouncementRule
from bot.notion.models import EventInfo
from bot.services.announcement_template import (
    DEFAULT_TEMPLATES,
    GENERAL,
    START,
    TemplateCheck,
    check_template,
)
from bot.timezone import get_timezone

ANNOUNCE_CHANNEL_KEY = "announcement_channel"
ANNOUNCE_GENERAL_KEY = "announcement_general_enabled"
ANNOUNCE_START_KEY = "announcement_start_enabled"
ANNOUNCE_GENERAL_TEMPLATE_KEY = "announcement_general_template"
ANNOUNCE_START_TEMPLATE_KEY = "announcement_start_template"
ANNOUNCE_GENERAL_SINCE_KEY = "announcement_general_since"

DEFAULT_RULE_DAYS = 3
DEFAULT_RULE_TIME = datetime.time(18, 0)

_ENABLED_KEYS = {GENERAL: ANNOUNCE_GENERAL_KEY, START: ANNOUNCE_START_KEY}
_TEMPLATE_KEYS = {GENERAL: ANNOUNCE_GENERAL_TEMPLATE_KEY, START: ANNOUNCE_START_TEMPLATE_KEY}


@dataclass(frozen=True)
class AnnouncementSettings:
    channel_id: int | None
    general_enabled: bool
    start_enabled: bool
    general_template: str
    start_template: str
    general_since: datetime.datetime | None


def _kind_keys(kind: str, keys: dict[str, str]) -> str:
    try:
        return keys[kind]
    except KeyError:
        raise ValueError(f"Unknown announcement kind: {kind!r}") from None


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() == "true"


def _int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _instant(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        value = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)


async def _template(db: asyncpg.Pool, guild_id: int, kind: str) -> str:
    raw = await config.get_value(db, guild_id, _TEMPLATE_KEYS[kind])
    return raw if raw and raw.strip() else DEFAULT_TEMPLATES[kind]


async def load_settings(db: asyncpg.Pool, guild_id: int) -> AnnouncementSettings:
    return AnnouncementSettings(
        channel_id=_int(await config.get_value(db, guild_id, ANNOUNCE_CHANNEL_KEY)),
        general_enabled=_flag(await config.get_value(db, guild_id, ANNOUNCE_GENERAL_KEY)),
        start_enabled=_flag(await config.get_value(db, guild_id, ANNOUNCE_START_KEY)),
        general_template=await _template(db, guild_id, GENERAL),
        start_template=await _template(db, guild_id, START),
        general_since=_instant(await config.get_value(db, guild_id, ANNOUNCE_GENERAL_SINCE_KEY)),
    )


async def set_channel(db: asyncpg.Pool, guild_id: int, channel_id: int | None) -> None:
    if channel_id is None:
        await config.delete_value(db, guild_id, ANNOUNCE_CHANNEL_KEY)
    else:
        await config.set_value(db, guild_id, ANNOUNCE_CHANNEL_KEY, str(channel_id))


async def set_enabled(
    db: asyncpg.Pool,
    guild_id: int,
    kind: str,
    on: bool,
    now: datetime.datetime | None = None,
) -> None:
    key = _kind_keys(kind, _ENABLED_KEYS)
    if kind == GENERAL and on:
        now = now or datetime.datetime.now(datetime.UTC)
        await config.set_value(db, guild_id, ANNOUNCE_GENERAL_SINCE_KEY, now.isoformat())
        if not await announcements.list_rules(db, guild_id):
            await announcements.add_rule(db, guild_id, DEFAULT_RULE_DAYS, DEFAULT_RULE_TIME)
    await config.set_value(db, guild_id, key, "true" if on else "false")


async def save_template(
    db: asyncpg.Pool, guild_id: int, kind: str, text: str, event: EventInfo | None = None
) -> TemplateCheck:
    key = _kind_keys(kind, _TEMPLATE_KEYS)
    if not text.strip():
        await config.delete_value(db, guild_id, key)
        return check_template(DEFAULT_TEMPLATES[kind], kind, None)
    check = check_template(text, kind, None)
    if check.ok and event is not None:
        check = check_template(text, kind, event)
    if check.ok:
        await config.set_value(db, guild_id, key, text)
    return check


def rule_post_time(event: EventInfo, rule: AnnouncementRule) -> datetime.datetime:
    day = event.date - datetime.timedelta(days=rule.days_before)
    return datetime.datetime.combine(day, rule.post_time.replace(tzinfo=None)).replace(
        tzinfo=get_timezone()
    )


def is_rule_due(
    event: EventInfo,
    rule: AnnouncementRule,
    settings: AnnouncementSettings,
    now: datetime.datetime,
) -> bool:
    post = rule_post_time(event, rule)
    if post > now or now >= event.slot_start(0):
        return False
    floor = rule.created_at
    if settings.general_since is not None and settings.general_since > floor:
        floor = settings.general_since
    return post > floor
