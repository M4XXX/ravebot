"""Booking rules: slot limit per DJ and event, cancellation cutoff.

Rules live in the config table (four keys per guild). Missing, unreadable or
out-of-range values fall back to the default of that field.
"""
import datetime
import logging
from dataclasses import dataclass
from typing import Any

from bot.db import config as config_db
from bot.notion.models import MAX_SLOTS, EventInfo

logger = logging.getLogger(__name__)

KEY_SLOT_LIMIT_ENABLED = "slot_limit_enabled"
KEY_SLOT_LIMIT_MAX = "slot_limit_max"
KEY_CANCEL_CUTOFF_ENABLED = "cancel_cutoff_enabled"
KEY_CANCEL_CUTOFF_MINUTES = "cancel_cutoff_minutes"
RULE_KEYS: tuple[str, ...] = (
    KEY_SLOT_LIMIT_ENABLED,
    KEY_SLOT_LIMIT_MAX,
    KEY_CANCEL_CUTOFF_ENABLED,
    KEY_CANCEL_CUTOFF_MINUTES,
)

MIN_SLOT_LIMIT = 1
MAX_SLOT_LIMIT = MAX_SLOTS
MAX_CUTOFF_MINUTES = 10080


@dataclass(frozen=True)
class BookingRules:
    slot_limit_enabled: bool = True
    slot_limit_max: int = 2
    cancel_cutoff_enabled: bool = True
    cancel_cutoff_minutes: int = 60


DEFAULT_RULES = BookingRules()


def utc_now() -> datetime.datetime:
    """Timezone-aware UTC now; the single clock hook for the rules."""
    return datetime.datetime.now(datetime.UTC)


def _parse_int(raw: str | None, low: int, high: int) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if low <= value <= high else None


def _parse_flag(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() != "false"


async def load_rules(db: Any, guild_id: int | None) -> BookingRules:
    """Read the rules for a guild; never raises."""
    if db is None or guild_id is None:
        return DEFAULT_RULES
    try:
        raw = {key: await config_db.get_value(db, guild_id, key) for key in RULE_KEYS}
    except Exception:
        logger.warning("Could not read booking rules; using defaults", exc_info=True)
        return DEFAULT_RULES
    d = DEFAULT_RULES
    limit = _parse_int(raw[KEY_SLOT_LIMIT_MAX], MIN_SLOT_LIMIT, MAX_SLOT_LIMIT)
    cutoff = _parse_int(raw[KEY_CANCEL_CUTOFF_MINUTES], 0, MAX_CUTOFF_MINUTES)
    return BookingRules(
        slot_limit_enabled=_parse_flag(raw[KEY_SLOT_LIMIT_ENABLED], d.slot_limit_enabled),
        slot_limit_max=d.slot_limit_max if limit is None else limit,
        cancel_cutoff_enabled=_parse_flag(
            raw[KEY_CANCEL_CUTOFF_ENABLED], d.cancel_cutoff_enabled
        ),
        cancel_cutoff_minutes=d.cancel_cutoff_minutes if cutoff is None else cutoff,
    )


async def current_rules(db: Any, bot: Any) -> BookingRules:
    """Rules of the first guild the bot is in (defaults without bot or guild)."""
    guilds = getattr(bot, "guilds", None) if bot is not None else None
    if not guilds:
        return DEFAULT_RULES
    return await load_rules(db, guilds[0].id)


async def save_rules(db: Any, guild_id: int, rules: BookingRules) -> None:
    values = {
        KEY_SLOT_LIMIT_ENABLED: "true" if rules.slot_limit_enabled else "false",
        KEY_SLOT_LIMIT_MAX: str(rules.slot_limit_max),
        KEY_CANCEL_CUTOFF_ENABLED: "true" if rules.cancel_cutoff_enabled else "false",
        KEY_CANCEL_CUTOFF_MINUTES: str(rules.cancel_cutoff_minutes),
    }
    for key, value in values.items():
        await config_db.set_value(db, guild_id, key, value)


def parse_rules_form(raw: dict[str, str]) -> tuple[BookingRules | None, list[str]]:
    """Validate form input; checkboxes count as on when the key is present."""
    errors: list[str] = []
    limit = _parse_int(raw.get(KEY_SLOT_LIMIT_MAX), MIN_SLOT_LIMIT, MAX_SLOT_LIMIT)
    if limit is None:
        errors.append(
            f"Slots per DJ and event must be a whole number from {MIN_SLOT_LIMIT} "
            f"to {MAX_SLOT_LIMIT}."
        )
    cutoff = _parse_int(raw.get(KEY_CANCEL_CUTOFF_MINUTES), 0, MAX_CUTOFF_MINUTES)
    if cutoff is None:
        errors.append(
            f"Cancellation cutoff must be a whole number of minutes from 0 to "
            f"{MAX_CUTOFF_MINUTES}."
        )
    if errors or limit is None or cutoff is None:
        return None, errors
    return (
        BookingRules(
            slot_limit_enabled=KEY_SLOT_LIMIT_ENABLED in raw,
            slot_limit_max=limit,
            cancel_cutoff_enabled=KEY_CANCEL_CUTOFF_ENABLED in raw,
            cancel_cutoff_minutes=cutoff,
        ),
        [],
    )


def slots_held(event: EventInfo, user_id: int) -> int:
    return sum(
        1
        for slot in event.resolved_slots().values()
        if slot.dj_page_id and slot.discord_id == user_id
    )


def at_slot_limit(event: EventInfo, user_id: int, rules: BookingRules) -> bool:
    if not rules.slot_limit_enabled:
        return False
    return slots_held(event, user_id) >= rules.slot_limit_max


def cancellation_closed(
    event: EventInfo, now: datetime.datetime, rules: BookingRules
) -> bool:
    if not rules.cancel_cutoff_enabled:
        return False
    try:
        start = event.slot_start(0)
    except Exception:
        logger.warning("Event %s has no readable start", event.event_id, exc_info=True)
        return False
    return now >= start - datetime.timedelta(minutes=rules.cancel_cutoff_minutes)


def describe_minutes(minutes: int) -> str:
    if minutes == 0:
        return "the start of the event"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def _slots(n: int) -> str:
    return f"{n} slot" if n == 1 else f"{n} slots"


def slot_limit_text(limit: int) -> str:
    return (
        f"You already hold {_slots(limit)} in this event, which is the limit. "
        "Cancel one of them or choose another event."
    )


def no_event_below_limit_text(limit: int) -> str:
    return (
        f"You already hold {_slots(limit)} in every open event, which is the limit. "
        "Cancel a slot first to book another."
    )


def cancellation_closed_text(minutes: int) -> str:
    return (
        f"Cancelling is closed {describe_minutes(minutes)} before the start. "
        "Please contact an admin."
        if minutes
        else "Cancelling is closed from the start of the event. Please contact an admin."
    )


def rules_summary(rules: BookingRules) -> list[str]:
    limit = (
        f"Slot limit: on, {rules.slot_limit_max} per event"
        if rules.slot_limit_enabled
        else "Slot limit: off"
    )
    cutoff = (
        f"Cancellation cutoff: on, {rules.cancel_cutoff_minutes} minutes before the start"
        if rules.cancel_cutoff_enabled
        else "Cancellation cutoff: off"
    )
    return [limit, cutoff]
