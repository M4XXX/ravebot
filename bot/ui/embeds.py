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


_TITLE_ICON = "\U0001f3b6"
_BOOKED_ICON = "\U0001f3a7"
_OPEN_ICON = "\u25ab\ufe0f"
_MAX_TITLE = 256


def lineup_title(event: EventInfo) -> str:
    """`Rave Night \u00b7 Saturday 14.11.2026` with an icon; the name is cut to fit 256 chars."""
    suffix = f" \u00b7 {texts.fmt_date(event)}"
    prefix = f"{_TITLE_ICON} "
    room = _MAX_TITLE - len(prefix) - len(suffix)
    name = texts.event_name(event)
    if len(name) > room:
        name = name[: room - 1] + "\u2026"
    return f"{prefix}{name}{suffix}"


def _lineup_summary(booked: int, total: int) -> str:
    if total > 0 and booked == total:
        return f"All {total} slot booked" if total == 1 else f"All {total} slots booked"
    noun = "slot" if total == 1 else "slots"
    return f"{booked} of {total} {noun} booked \u00b7 {total - booked} open"


def lineup_rows(event: EventInfo) -> list[tuple[str, str, str]]:
    """One (slot, time, DJ) cell triple per slot; each cell is a single line."""
    by_number = event.resolved_slots()
    rows: list[tuple[str, str, str]] = []
    for index in range(event.slot_count):
        slot = by_number.get(index + 1)
        if slot is not None and slot.dj_page_id is not None:
            dj = f"{_BOOKED_ICON} {_safe_dj_name(slot.dj_name)}"
        else:
            dj = f"{_OPEN_ICON} *open slot*"
        rows.append((f"**Slot {index + 1}**", texts.slot_time(event, index), dj))
    return rows


def lineup_text(event: EventInfo, hidden: int = 0) -> str:
    """Lineup description: meta line, summary, a "not shown" note, notes; at most 4096 chars.

    The slot rows are the embed's columns (see LineupUpdateEmbed). An overlong Notes
    block is dropped, never cut.
    """
    by_number = event.resolved_slots()
    booked = sum(
        1
        for index in range(event.slot_count)
        if (slot := by_number.get(index + 1)) is not None and slot.dj_page_id is not None
    )
    meta = texts.event_meta(event)
    head = [meta, ""] if meta else []
    head.append(_lineup_summary(booked, event.slot_count))
    if hidden:
        head.append(f"{hidden} slot not shown" if hidden == 1 else f"{hidden} slots not shown")
    note = (event.notes or "").strip()
    notes_block = ["", f"**{texts.NOTES_HEADING}**", texts.escape_text(note)] if note else []
    text = "\n".join([*head, *notes_block])
    return text if len(text) <= _MAX_DESCRIPTION else "\n".join(head)


_FIELD_MAX = 1024
_EMBED_MAX = 6000
_EMBED_MARGIN = 100  # room for the "not shown" line and rounding
_MAX_COLUMN_SETS = 8  # three fields each; one more field is left for the Events list
_BLANK = "\u200b"
_COLUMN_NAMES = (texts.LINEUP_COL_SLOT, texts.LINEUP_COL_TIME, texts.LINEUP_COL_DJ)


def _column_sets(
    rows: list[tuple[str, str, str]], budget: int
) -> tuple[list[list[tuple[str, str, str]]], int]:
    """Group rows into sets of three columns that fit the field and embed limits.

    Returns (sets, number of rows that did not fit).
    """
    sets: list[list[tuple[str, str, str]]] = []
    used = 0
    taken = 0
    for row in rows:
        cost = sum(len(cell) + 1 for cell in row)
        current = sets[-1] if sets else None
        fits_set = current is not None and all(
            sum(len(r[i]) + 1 for r in current) + len(row[i]) + 1 <= _FIELD_MAX for i in range(3)
        )
        if not fits_set:
            if len(sets) >= _MAX_COLUMN_SETS:
                break
            cost += len(_COLUMN_NAMES[0] + _COLUMN_NAMES[1] + _COLUMN_NAMES[2])
        if used + cost > budget:
            break
        used += cost
        taken += 1
        if fits_set and current is not None:
            current.append(row)
        else:
            sets.append([row])
    return sets, len(rows) - taken


class LineupUpdateEmbed(disnake.Embed):
    """Public lineup post for one event: slots, times and DJs as three aligned columns."""

    def __init__(self, event: EventInfo, shown: list[EventInfo] | None = None) -> None:
        title = lineup_title(event)
        events_value = ""
        if shown:
            lines = [
                texts.event_pick_line(i, other, other.page_id == event.page_id)
                for i, other in enumerate(shown)
            ]
            events_value = "\n".join(lines)[:_FIELD_MAX]
        rows = lineup_rows(event)
        budget = (
            _EMBED_MAX
            - _EMBED_MARGIN
            - len(title)
            - len(lineup_text(event))
            - len(event.event_id)
            - len(events_value)
            - len(texts.EVENTS_FIELD)
        )
        sets, hidden = _column_sets(rows, budget)
        super().__init__(
            title=title,
            description=lineup_text(event, hidden),
            colour=EmbedColors.MISSION_GOLD,
        )
        for position, group in enumerate(sets):
            for column in range(3):
                self.add_field(
                    name=_COLUMN_NAMES[column] if position == 0 else _BLANK,
                    value="\n".join(row[column] for row in group),
                    inline=True,
                )
        if events_value:
            self.add_field(name=texts.EVENTS_FIELD, value=events_value, inline=False)
        self.set_footer(text=event.event_id)


class BookingEventPickEmbed(disnake.Embed):
    """Event picker overview: one block per event with its date and open slots."""

    def __init__(self, events: list[EventInfo], open_counts: list[int]) -> None:
        blocks = []
        for position, (event, open_count) in enumerate(zip(events, open_counts, strict=True)):
            noun = "slot" if event.slot_count == 1 else "slots"
            lines = [
                f"**{texts.event_button(position)} \u00b7 {texts.event_name(event)}**",
                texts.ts(event.slot_start(0), "F"),
            ]
            meta = texts.event_meta(event)
            if meta:
                lines.append(meta)
            lines.append(f"{_BOOKED_ICON} {open_count} of {event.slot_count} {noun} open")
            blocks.append("\n".join(lines))
        super().__init__(
            title=texts.PICK_EVENT,
            description="\n\n".join(blocks),
            colour=EmbedColors.MISSION_GOLD,
        )


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
