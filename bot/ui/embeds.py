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

from bot import schedule, slots
from bot.notion import props


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
    ERR_BACKGROUND_TASK = "ERR_BACKGROUND_TASK"
    ERR_DATA_MISMATCH = "ERR_DATA_MISMATCH"


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


class MissionRosterEmbed(disnake.Embed):
    """Mission Gold lineup embed — lists upcoming events with slot status.

    Posted to the configured DJ channel on each lineup change (at most once/day).
    Edit-in-place on subsequent changes; delete-and-repost if edit fails.

    [Source: ux-design-specification.md#MissionRosterEmbed]
    [Source: epics.md#Story 3.2 AC1]
    """

    def __init__(self, events: list[dict]) -> None:
        super().__init__(
            color=EmbedColors.MISSION_GOLD,
            title="\U0001f30c Mission Roster",
            timestamp=datetime.now(tz=UTC),
        )
        if not events:
            self.description = "No upcoming events scheduled."
        for event in events:
            try:
                name = props.title_text(event)
            except (KeyError, IndexError):
                name = "Unnamed Event"
            start = schedule.event_start(event)
            date_field = f"<t:{int(start.timestamp())}:f>" if start is not None else "TBD"
            st = slots.state(event)
            field_value = f"{date_field}\n{st.booked_count} confirmed · {len(st.open_indices)} open"
            if not st.bookable:  # tds layout: the status is outside the planning phase
                field_value += " · not open for booking"
            self.add_field(name=name, value=field_value, inline=False)
        self.set_footer(text="RaveBot")


class MissionOverviewEmbed(disnake.Embed):
    """Deep Space Blue embed — per-event slot display for Show Lineup flow.

    Shows a single upcoming event with each slot's time (computed as
    event_start + slot_index * 60 min) and booking status (Confirmed/Open).
    DJ names are count-based at this stage — individual DJ lookup deferred to Story 4.x.

    Used by: FallbackMenuView.show_lineup() (Story 3.3)
    Future: proactive outreach, self-service booking (Epic 4, 5)

    [Source: ux-design-specification.md#MissionOverviewEmbed]
    [Source: epics.md#Story 3.3 AC1–AC4]
    """

    def __init__(self, event: dict) -> None:
        super().__init__(
            color=EmbedColors.DEEP_SPACE_BLUE,
            timestamp=datetime.now(tz=UTC),
        )
        # Parse event name
        try:
            name = props.title_text(event)
        except (KeyError, IndexError):
            name = "Unknown Event"
        self.title = f"\U0001f6f0\ufe0f {name}"  # 🛰️

        # Parse event start time for slot time computation
        event_start = schedule.event_start(event)

        # Slot counts and booked indices (bot.slots: Postgres slot_locks)
        st = slots.state(event)
        slots_total, djs_booked, slots_open = st.total, st.booked_count, len(st.open_indices)

        date_display = (
            f"<t:{int(event_start.timestamp())}:f>" if event_start is not None else "TBD"
        )
        self.description = (
            f"{date_display} · All slots filled"
            if slots_open == 0
            else f"{date_display} · {djs_booked}/{slots_total} confirmed · {slots_open} open"
        )

        for i in range(slots_total):
            if event_start is not None:
                unix_ts = schedule.slot_unix(event, i)
                time_str = f"<t:{unix_ts}:R>"
            else:
                time_str = "TBD"
            if i in st.names:  # tds layout: the name in the plan, also when typed by hand
                status = f"✓ {st.names[i]}"
            else:
                status = "✓ Confirmed" if i in st.booked else "Open"
            self.add_field(
                name=f"Slot {i + 1}",
                value=f"{status} · {time_str}",
                inline=True,
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

    def __init__(self, event: dict) -> None:
        super().__init__(
            color=EmbedColors.DEEP_SPACE_BLUE,
            title="\U0001f30c Mission Opportunity",  # 🌌
            timestamp=datetime.now(tz=UTC),
        )
        try:
            name = props.title_text(event)
        except (KeyError, IndexError):
            name = "Upcoming Event"
        start = schedule.event_start(event)
        date_display = f"<t:{int(start.timestamp())}:f>" if start is not None else "TBD"
        st = slots.state(event)
        slots_total, open_slots = st.total, len(st.open_indices)

        self.add_field(name="Event", value=name, inline=True)
        self.add_field(name="Date", value=date_display, inline=True)
        self.add_field(
            name="Open Slots",
            value=f"{open_slots} of {slots_total}",
            inline=True,
        )
        self.set_footer(text="RaveBot")
