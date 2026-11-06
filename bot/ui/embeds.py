"""Embed classes and EmbedColors design system.

RULE: All embed construction MUST reference EmbedColors constants — never inline hex values.

Classes:
    EmbedColors            — named color constants (Story 1.1 stub, completed Story 1.3)
    ErrorCode              — six error code strings for AdminLogService.post_error()
    ErrorEmbed             — red embed for system errors posted to admin log channel
    AdminLogEmbed          — white embed for DJ interaction transcripts
    MissionRosterEmbed     — Mission Gold lineup embed (Story 3.2)
    BookingConfirmEmbed    — Mission Gold pre-confirmation embed (Story 4.2)
    BookedEmbed            — post-booking embed
"""
import enum
from datetime import UTC, datetime

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


class ErrorEmbed(disnake.Embed):
    """Red admin-log embed for system errors.

    Posts to the admin log channel via AdminLogService.post_error().
    Always includes the error code as a text title alongside the red color accent —
    no color-only signaling (UX-DR18).

    Fields: Trigger Condition, Action Taken, Manual Action Required.
    Footer: "RaveBot" with ISO timestamp.

    [Source: ux-design-specification.md#ErrorEmbed]
    [Source: epics.md#UX-DR11, UX-DR18]
    """

    def __init__(
        self,
        error_code: ErrorCode,
        trigger: str,
        action_taken: str,
        manual_action: str = "No manual action required.",
    ) -> None:
        super().__init__(
            color=EmbedColors.IMPERIAL_RED,
            title=error_code.value,  # text label alongside color — satisfies UX-DR18
            timestamp=datetime.now(tz=UTC),
        )
        self.add_field(name="Trigger Condition", value=trigger, inline=False)
        self.add_field(name="Action Taken", value=action_taken, inline=False)
        self.add_field(name="Manual Action Required", value=manual_action, inline=False)
        self.set_footer(text="RaveBot")


class AdminLogEmbed(disnake.Embed):
    """White admin-log embed for DJ interaction transcripts.

    Posts to the admin log channel via AdminLogService.post_interaction().
    Consistent field layout across all variants: Booking, Cancellation,
    Outreach, Onboarding, Intervention.
    Color (Nebula White) combined with title text — satisfies UX-DR18.

    Fields: DJ, Action, Outcome, and optionally Event.
    Footer: "RaveBot" with ISO timestamp.

    [Source: ux-design-specification.md#AdminLogEmbed]
    [Source: epics.md#UX-DR10, UX-DR18]
    """

    def __init__(
        self,
        dj_name: str,
        action_type: str,
        outcome: str,
        event: str | None = None,
    ) -> None:
        super().__init__(
            color=EmbedColors.NEBULA_WHITE,
            title=f"\U0001f4cb DM Log \u00b7 {dj_name} \u2192 {action_type}",
            timestamp=datetime.now(tz=UTC),
        )
        self.add_field(name="DJ", value=dj_name, inline=True)
        self.add_field(name="Action", value=action_type, inline=True)
        self.add_field(name="Outcome", value=outcome, inline=True)
        if event is not None:
            self.add_field(name="Event", value=event, inline=False)
        self.set_footer(text="RaveBot")


def _slot_counts(event: EventInfo) -> tuple[int, int, int]:
    """Return (booked, total, open) for an event; a DJ with two slots counts two."""
    total = event.slot_count
    booked = min(sum(1 for slot in event.slots if slot.dj_page_id is not None), total)
    return booked, total, total - booked


_MAX_FIELDS = 25
_SHOWN_WHEN_CAPPED = _MAX_FIELDS - 1
_MAX_DJ_NAME = 100
_MAX_FIELD_NAME = 256  # Discord's limit for an embed field name


def _field_name(text: str) -> str:
    """Cap text used as an embed field name at Discord's 256 characters."""
    if len(text) <= _MAX_FIELD_NAME:
        return text
    return text[: _MAX_FIELD_NAME - 1] + "\u2026"


def _safe_dj_name(name: str | None) -> str:
    """DJ-controlled text rendered literally: truncated, markdown/mentions/tokens escaped."""
    if not name:
        return "Guest DJ"
    truncated = len(name) > _MAX_DJ_NAME
    text = name[:_MAX_DJ_NAME]
    text = disnake.utils.escape_mentions(disnake.utils.escape_markdown(text))
    text = text.replace("<", "<\u200b")  # break <@id>, <t:..>, <#id> tokens
    return text + "\u2026" if truncated else text


class MissionRosterEmbed(disnake.Embed):
    """Mission Gold lineup embed — lists upcoming events with slot status.

    Posted to the configured DJ channel on each lineup change (at most once/day).
    Edit-in-place on subsequent changes; delete-and-repost if edit fails.

    [Source: ux-design-specification.md#MissionRosterEmbed]
    [Source: epics.md#Story 3.2 AC1]
    """

    def __init__(self, events: list[EventInfo]) -> None:
        super().__init__(
            color=EmbedColors.MISSION_GOLD,
            title="\U0001f30c Mission Roster",
            timestamp=datetime.now(tz=UTC),
        )
        if not events:
            self.description = "No upcoming events scheduled."
        capped = len(events) > _MAX_FIELDS
        shown = events[:_SHOWN_WHEN_CAPPED] if capped else events
        for event in shown:
            booked, total, slots_open = _slot_counts(event)
            date_field = f"<t:{int(event.slot_start(0).timestamp())}:f>"
            field_value = f"{date_field}\n{booked} confirmed · {slots_open} open"
            self.add_field(name=_field_name(event.event_id), value=field_value, inline=False)
        if capped:
            self.add_field(
                name="More events",
                value=f"{len(events) - len(shown)} more events not shown.",
                inline=False,
            )
        self.set_footer(text="RaveBot")


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
    by_number = {slot.number: slot for slot in event.slots}
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
        note = f"{hidden} slots not shown"
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
