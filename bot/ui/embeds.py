"""Embed classes and EmbedColors design system.

RULE: All embed construction MUST reference EmbedColors constants — never inline hex values.

Classes:
    EmbedColors   — named color constants (Story 1.1 stub, completed Story 1.3)
    ErrorCode     — six error code strings for AdminLogService.post_error()
    ErrorEmbed    — red embed for system errors posted to admin log channel
    AdminLogEmbed — white embed for DJ interaction transcripts
"""
import enum
from datetime import UTC, datetime

import disnake


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
