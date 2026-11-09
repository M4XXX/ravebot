"""Embed classes and EmbedColors design system.

RULE: All embed construction MUST reference EmbedColors constants — never inline hex values.

Classes:
    EmbedColors            — named color constants (Story 1.1 stub, completed Story 1.3)
    ErrorCode              — six error code strings for AdminLogService.post_error()
    LogEntryEmbed          — one log channel entry: title, plain text, optional "What to do" field
    BookingConfirmEmbed    — Mission Gold pre-confirmation embed (Story 4.2)
    BookedEmbed            — post-booking embed
    LineupUpdateEmbed      — "Lineup Update" post for the DJ channel (one per change)
"""
import enum
import re

import disnake

from bot.notion.models import EventInfo
from bot.ui import texts


class EmbedColors(enum.IntEnum):
    """Named color constants for all RaveBot embeds.

    Usage: color=EmbedColors.MISSION_GOLD  (never color=0xFFD700)
    """

    MISSION_GOLD = 0xFFD700       # Booking confirmations, lineup posts, reminders
    IMPERIAL_RED = 0xED4245       # Errors, DM failures, outreach exhausted, DB unavailable
    DEEP_SPACE_BLUE = 0x5865F2   # Outreach in progress, status updates, informational
    STARLIGHT_YELLOW = 0xFEE75C  # Pending, awaiting DJ response
    NEBULA_WHITE = 0xF5F5F5      # Neutral system messages, admin log entries
    SUCCESS_GREEN = 0x2E7D32     # Log entries: booked, event fully booked, new DJ


class ErrorCode(enum.StrEnum):
    """Centralised error taxonomy for AdminLogService.post_error() calls.

    StrEnum members are their own string value — usable directly as embed titles.
    [Source: architecture.md#Error Routing: Centralized AdminLogService]
    [Source: epics.md#Additional Requirements — error codes]
    """

    ERR_DM_FAILED = "ERR_DM_FAILED"
    ERR_NOTION_WRITE = "ERR_NOTION_WRITE"
    ERR_SLOT_CONFLICT = "ERR_SLOT_CONFLICT"
    ERR_BUTTON_TIMEOUT = "ERR_BUTTON_TIMEOUT"
    ERR_DB_UNAVAILABLE = "ERR_DB_UNAVAILABLE"
    ERR_OUTREACH_EXHAUSTED = "ERR_OUTREACH_EXHAUSTED"


_MAX_DJ_NAME = 100


def _safe_dj_name(name: str | None) -> str:
    """DJ-controlled text rendered literally: truncated, markdown/mentions/tokens escaped."""
    if not name:
        return "Guest DJ"
    truncated = len(name) > _MAX_DJ_NAME
    text = name[:_MAX_DJ_NAME]
    text = disnake.utils.escape_mentions(disnake.utils.escape_markdown(text))
    text = text.replace("<", "<\u200b")  # break <@id>, <t:..>, <#id> tokens
    return text + "\u2026" if truncated else text


class BookingConfirmEmbed(disnake.Embed):
    """Confirmation embed shown before the DJ taps Confirm."""

    def __init__(self, event: EventInfo, slot_index: int) -> None:
        super().__init__(
            color=EmbedColors.MISSION_GOLD,
            title=texts.CONFIRM_TITLE,
            description=texts.booking_body(event, slot_index),
        )


class BookedEmbed(disnake.Embed):
    """Embed sent after a successful booking."""

    def __init__(self, event: EventInfo, slot_index: int) -> None:
        super().__init__(
            color=EmbedColors.MISSION_GOLD,
            title=texts.BOOKED_TITLE,
            description=f"{texts.booking_body(event, slot_index)}\n{texts.BOOKED_REMINDER_NOTE}",
        )


_MAX_DESCRIPTION = 4096


_TITLE_ICON = "\U0001f3b6"
_BOOKED_ICON = "\U0001f3a7"
_OPEN_ICON = "\u25ab\ufe0f"
_MAX_TITLE = 256
_DATED_ID = re.compile(r"^\d{4}-\d{2}-\d{2}\s+(?P<name>.+)$")


def _event_name(event: EventInfo) -> str:
    """The event ID without its leading date (`2026-11-14 Rave Night` becomes `Rave Night`)."""
    match = _DATED_ID.match(event.event_id)
    return match.group("name") if match else event.event_id


def lineup_title(event: EventInfo) -> str:
    """`Rave Night \u00b7 Saturday 14.11.2026` with an icon; the name is cut to fit 256 chars."""
    suffix = f" \u00b7 {texts.fmt_date(event)}"
    prefix = f"{_TITLE_ICON} "
    room = _MAX_TITLE - len(prefix) - len(suffix)
    name = _event_name(event)
    if len(name) > room:
        name = name[: room - 1] + "\u2026"
    return f"{prefix}{name}{suffix}"


def _lineup_summary(booked: int, total: int) -> str:
    if total > 0 and booked == total:
        return f"All {total} slot booked" if total == 1 else f"All {total} slots booked"
    noun = "slot" if total == 1 else "slots"
    return f"{booked} of {total} {noun} booked \u00b7 {total - booked} open"


def lineup_text(event: EventInfo) -> str:
    """Lineup body: a summary line, then one line per slot; whole lines, capped at 4096 chars."""
    by_number = event.resolved_slots()
    slot_lines: list[str] = []
    booked = 0
    for index in range(event.slot_count):
        slot = by_number.get(index + 1)
        time = f"**{texts.fmt_time(event, index)}**"
        if slot is not None and slot.dj_page_id is not None:
            booked += 1
            slot_lines.append(f"{time}  {_BOOKED_ICON} {_safe_dj_name(slot.dj_name)}")
        else:
            slot_lines.append(f"{time}  {_OPEN_ICON} *open slot*")
    head = [_lineup_summary(booked, event.slot_count), ""]
    text = "\n".join([*head, *slot_lines])
    if len(text) <= _MAX_DESCRIPTION:
        return text
    for keep in range(len(slot_lines), -1, -1):
        hidden = len(slot_lines) - keep
        note = f"{hidden} slot not shown" if hidden == 1 else f"{hidden} slots not shown"
        candidate = "\n".join([*head, *slot_lines[:keep], note])
        if len(candidate) <= _MAX_DESCRIPTION:
            return candidate
    return head[0]


class LineupUpdateEmbed(disnake.Embed):
    """Public lineup post for one event."""

    def __init__(self, event: EventInfo) -> None:
        super().__init__(
            title=lineup_title(event),
            description=lineup_text(event),
            colour=EmbedColors.MISSION_GOLD,
        )
        self.set_footer(text=event.event_id)


_CUT_NOTE = "\n(cut)"


class LogEntryEmbed(disnake.Embed):
    """One typed entry for the admin log channel."""

    def __init__(
        self, title: str, description: str, color: EmbedColors, todo: str | None = None
    ) -> None:
        if len(description) > _MAX_DESCRIPTION:
            description = description[: _MAX_DESCRIPTION - len(_CUT_NOTE)] + _CUT_NOTE
        super().__init__(title=title[:256], description=description, color=color)
        if todo:
            self.add_field(name=texts.LOG_TODO_FIELD, value=todo[:1024], inline=False)
        self.set_footer(text="RaveBot")
