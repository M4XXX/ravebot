"""Embed classes and EmbedColors design system.

RULE: All embed construction MUST reference EmbedColors constants — never inline hex values.

Classes:
    EmbedColors            — named color constants (Story 1.1 stub, completed Story 1.3)
    ErrorCode              — six error code strings for AdminLogService.post_error()
    ErrorEmbed             — red embed for system errors posted to admin log channel
    AdminLogEmbed          — white embed for DJ interaction transcripts
    MissionRosterEmbed     — Mission Gold lineup embed (Story 3.2)
    MissionOverviewEmbed   — Deep Space Blue per-event slot detail embed (Story 3.3)
    BookingConfirmEmbed    — Mission Gold pre-confirmation embed (Story 4.2)
    BookingConfirmedEmbed  — Mission Gold post-confirmation embed (Story 4.2)
    ProactiveOutreachEmbed — Deep Space Blue outreach invitation embed (Story 5.2)
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


class MissionOverviewEmbed(disnake.Embed):
    """Deep Space Blue embed — per-event slot display for Show Lineup flow.

    Shows a single upcoming event with each slot's start (event Start plus
    slot_index * the event's Slot length) and the slot's DJ name, or "Open".
    The description counts booked and open slots; more than 25 slots are
    summarised in a final "More slots" field.

    Used by: FallbackMenuView.show_lineup() (Story 3.3)
    Future: proactive outreach, self-service booking (Epic 4, 5)

    [Source: ux-design-specification.md#MissionOverviewEmbed]
    [Source: epics.md#Story 3.3 AC1–AC4]
    """

    def __init__(self, event: EventInfo) -> None:
        super().__init__(
            color=EmbedColors.DEEP_SPACE_BLUE,
            timestamp=datetime.now(tz=UTC),
        )
        self.title = f"\U0001f6f0\ufe0f {event.event_id}"  # 🛰️

        booked, slots_total, slots_open = _slot_counts(event)
        date_display = f"<t:{int(event.slot_start(0).timestamp())}:f>"
        if slots_total == 0:
            self.description = f"{date_display} · No slots."
        elif slots_open == 0:
            self.description = f"{date_display} · All slots filled"
        else:
            self.description = (
                f"{date_display} · {booked}/{slots_total} confirmed · {slots_open} open"
            )

        by_number = {slot.number: slot for slot in event.slots}
        shown_slots = _SHOWN_WHEN_CAPPED if slots_total > _MAX_FIELDS else slots_total
        for i in range(shown_slots):
            time_str = f"<t:{int(event.slot_start(i).timestamp())}:R>"
            slot = by_number.get(i + 1)
            if slot is not None and slot.dj_page_id is not None:
                status = _safe_dj_name(slot.dj_name)
            else:
                status = "Open"
            self.add_field(
                name=f"Slot {i + 1}",
                value=f"{status} · {time_str}",
                inline=True,
            )
        if slots_total > _MAX_FIELDS:
            hidden = slots_total - shown_slots
            hidden_open = sum(
                1
                for n in range(shown_slots + 1, slots_total + 1)
                if n not in by_number or by_number[n].dj_page_id is None
            )
            self.add_field(
                name="More slots",
                value=f"{hidden} more slots not shown · {hidden_open} open",
                inline=False,
            )
        self.set_footer(text="RaveBot")


class BookingConfirmEmbed(disnake.Embed):
    """Mission Gold confirmation embed — shown before the DJ taps [Confirm Booking].

    Displays the event name and selected slot time so the DJ can verify before
    committing. Shown alongside BookingConfirmView buttons.

    [Source: ux-design-specification.md#UX-DR4]
    [Source: epics.md#Story 4.2 AC1]
    """

    def __init__(self, event_name: str, slot_unix: int) -> None:
        super().__init__(
            color=EmbedColors.MISSION_GOLD,
            title="Confirm Your Booking",
            timestamp=datetime.now(tz=UTC),
        )
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "TBD"
        self.add_field(name="Event", value=event_name, inline=True)
        self.add_field(name="Slot", value=slot_display, inline=True)
        self.set_footer(text="RaveBot")


class BookingConfirmedEmbed(disnake.Embed):
    """Mission Gold booking confirmation embed — sent after a successful booking.

    UX-DR5: "Slot Secured" title, event name + Discord timestamp, day-before reminder note.
    Footer format: "RaveBot · {event_name}".

    [Source: ux-design-specification.md#UX-DR5]
    [Source: epics.md#Story 4.2 AC3]
    """

    def __init__(self, event_name: str, slot_unix: int) -> None:
        super().__init__(
            color=EmbedColors.MISSION_GOLD,
            title="Slot Secured",
            timestamp=datetime.now(tz=UTC),
        )
        slot_display = f"<t:{slot_unix}:f>" if slot_unix else "TBD"
        self.add_field(name="Event", value=event_name, inline=True)
        self.add_field(name="Your Slot", value=slot_display, inline=True)
        self.add_field(
            name="Heads Up",
            value="I'll send you a reminder the day before. See you on the decks! \U0001f3a7",
            inline=False,
        )
        self.set_footer(text=f"RaveBot \u00b7 {event_name}")


class ProactiveOutreachEmbed(disnake.Embed):
    """Deep Space Blue embed sent alongside proactive outreach DMs.

    Displays structured event details (name, date, open slots) to accompany
    the plain-text opener in the DM body.

    Color: DEEP_SPACE_BLUE — outreach in progress status indicator.
    Always includes both color AND text title — satisfies UX-DR18.

    [Source: epics.md#Story 5.2 AC1]
    [Source: ux-design-specification.md#UX-DR2]
    [Source: epics.md#UX-DR14 — all times as Discord timestamps]
    [Source: epics.md#UX-DR18 — color + text label, never color-only]
    """

    def __init__(self, event: EventInfo) -> None:
        super().__init__(
            color=EmbedColors.DEEP_SPACE_BLUE,
            title="\U0001f30c Mission Opportunity",  # 🌌
            timestamp=datetime.now(tz=UTC),
        )
        _, slots_total, open_slots = _slot_counts(event)
        date_display = f"<t:{int(event.slot_start(0).timestamp())}:f>"

        self.add_field(name="Event", value=event.event_id, inline=True)
        self.add_field(name="Date", value=date_display, inline=True)
        self.add_field(
            name="Open Slots",
            value=f"{open_slots} of {slots_total}",
            inline=True,
        )
        self.set_footer(text="RaveBot")


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
