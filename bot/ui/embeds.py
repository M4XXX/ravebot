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


def lineup_text(event: EventInfo) -> str:
    """Lineup body: event line plus one line per slot; whole lines, capped at 4096 chars."""
    by_number = event.resolved_slots()
    lines = [f"Event {texts.fmt_date(event)}"]
    for index in range(event.slot_count):
        slot = by_number.get(index + 1)
        holder = (
            _safe_dj_name(slot.dj_name)
            if slot is not None and slot.dj_page_id is not None
            else "OPEN SLOT"
        )
        lines.append(f"Slot {index + 1}  {texts.fmt_time(event, index)}  {holder}")
    text = "\n".join(lines)
    if len(text) <= _MAX_DESCRIPTION:
        return text
    slot_lines = lines[1:]
    for keep in range(len(slot_lines), -1, -1):
        hidden = len(slot_lines) - keep
        note = f"{hidden} slot not shown" if hidden == 1 else f"{hidden} slots not shown"
        candidate = "\n".join([lines[0], *slot_lines[:keep], note])
        if len(candidate) <= _MAX_DESCRIPTION:
            return candidate
    return lines[0]


class LineupUpdateEmbed(disnake.Embed):
    """Public lineup post for one event."""

    def __init__(self, event: EventInfo) -> None:
        super().__init__(
            title="Lineup Update",
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
