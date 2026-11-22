"""Tests for bot/ui/embeds.py — EmbedColors, ErrorCode."""
import asyncio
import datetime
import re
from unittest.mock import MagicMock

import pytest

from bot.notion.models import EventInfo
from bot.ui import texts
from bot.ui.embeds import (
    EmbedColors,
    ErrorCode,
)
from tests.notion.factories import make_event, make_slot


class TestEmbedColors:
    def test_all_constants_defined(self) -> None:
        """All five named color constants exist."""
        expected = {
            "MISSION_GOLD",
            "IMPERIAL_RED",
            "DEEP_SPACE_BLUE",
            "STARLIGHT_YELLOW",
            "NEBULA_WHITE",
            "SUCCESS_GREEN",
        }
        assert {c.name for c in EmbedColors} == expected

    def test_hex_values(self) -> None:
        """Each constant has the correct hex value."""
        assert EmbedColors.MISSION_GOLD == 0xFFD700
        assert EmbedColors.IMPERIAL_RED == 0xED4245
        assert EmbedColors.DEEP_SPACE_BLUE == 0x5865F2
        assert EmbedColors.STARLIGHT_YELLOW == 0xFEE75C
        assert EmbedColors.NEBULA_WHITE == 0xF5F5F5

    def test_is_int_enum(self) -> None:
        """EmbedColors members are valid integers (usable as disnake color= values)."""
        for color in EmbedColors:
            assert isinstance(color, int)


class TestErrorCode:
    def test_all_six_codes_defined(self) -> None:
        """All six error codes are defined."""
        names = {c.name for c in ErrorCode}
        assert names == {
            "ERR_DM_FAILED",
            "ERR_NOTION_WRITE",
            "ERR_SLOT_CONFLICT",
            "ERR_BUTTON_TIMEOUT",
            "ERR_DB_UNAVAILABLE",
            "ERR_OUTREACH_EXHAUSTED",
        }

    def test_error_code_is_str(self) -> None:
        """ErrorCode members are strings (usable as embed titles)."""
        for code in ErrorCode:
            assert isinstance(code, str)


def _booked_event(
    name: str = "Friday Night Rave",
    slots: int = 6,
    djs: int = 2,
    date: datetime.date = datetime.date(2026, 6, 13),
) -> EventInfo:
    return make_event(
        event_id=name,
        date=date,
        slot_count=slots,
        slots=[
            make_slot(i + 1, dj_page_id=f"dj-{i}", dj_name=f"DJ {i}") for i in range(djs)
        ],
    )


class TestEmbedHardening:
    def test_safe_dj_name_none_is_guest(self) -> None:
        from bot.ui.embeds import _safe_dj_name

        assert _safe_dj_name(None) == "Guest DJ"


class TestBookingConfirmEmbed:
    def test_uses_mission_gold(self) -> None:
        from bot.ui.embeds import BookingConfirmEmbed
        embed = BookingConfirmEmbed(make_event(), 0)
        assert embed.color.value == EmbedColors.MISSION_GOLD

    def test_title_and_body(self) -> None:
        from bot.ui.embeds import BookingConfirmEmbed
        embed = BookingConfirmEmbed(make_event(event_id="Test Rave"), 0)
        assert embed.title == "Confirm your booking"
        when = texts.slot_when(make_event(event_id="Test Rave"), 0)
        assert embed.description == f"Test Rave\nSlot 1, {when}"


class TestBookedEmbed:
    def test_uses_mission_gold(self) -> None:
        from bot.ui.embeds import BookedEmbed
        embed = BookedEmbed(make_event(), 0)
        assert embed.color.value == EmbedColors.MISSION_GOLD

    def test_title_and_body(self) -> None:
        from bot.ui.embeds import BookedEmbed
        embed = BookedEmbed(make_event(event_id="Test Rave"), 1)
        assert embed.title == "You're booked"
        when = texts.slot_when(make_event(event_id="Test Rave"), 1)
        assert embed.description == (
            f"Test Rave\nSlot 2, {when}\nI'll remind you the day before."
        )


class TestLineupUpdateEmbed:
    def _event(self, slots, slot_count=3):
        return make_event(
            event_id="EV-1",
            date=datetime.date(2026, 11, 14),
            start=datetime.time(18, 0),
            slot_count=slot_count,
            slots=slots,
        )

    def test_lineup_text_is_summary_only(self) -> None:
        from bot.ui.embeds import lineup_text

        event = self._event(
            [make_slot(1), make_slot(2, dj_page_id="d1", dj_name="Nova")]
        )
        assert lineup_text(event) == "1 of 3 slots booked \u00b7 2 open"

    def test_rows_are_one_line_per_slot_in_three_columns(self) -> None:
        from bot.ui.embeds import lineup_rows

        event = self._event(
            [make_slot(1), make_slot(2, dj_page_id="d1", dj_name="Nova")]
        )
        t = [texts.slot_time(event, i) for i in range(3)]
        assert lineup_rows(event) == [
            ("**Slot 1**", t[0], "\u25ab\ufe0f *open slot*"),
            ("**Slot 2**", t[1], "\U0001f3a7 Nova"),
            ("**Slot 3**", t[2], "\u25ab\ufe0f *open slot*"),
        ]

    def test_embed_shows_slots_as_aligned_columns(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed, lineup_rows

        event = self._event(
            [make_slot(1), make_slot(2, dj_page_id="d1", dj_name="Nova")]
        )
        embed = LineupUpdateEmbed(event)
        rows = lineup_rows(event)
        columns = [f for f in embed.fields if f.name in ("Slot", "Time", "DJ")]
        assert [f.name for f in columns] == ["Slot", "Time", "DJ"]
        assert all(f.inline for f in columns)
        for index, field in enumerate(columns):
            assert field.value.split("\n") == [row[index] for row in rows]
        assert "**Slot" not in (embed.description or "")

    def test_summary_when_fully_booked_and_when_empty_single_slot(self) -> None:
        from bot.ui.embeds import lineup_text

        full = self._event(
            [make_slot(i, dj_page_id=f"d{i}", dj_name=f"DJ{i}") for i in (1, 2, 3)]
        )
        assert lineup_text(full).splitlines()[0] == "All 3 slots booked"
        single = self._event([], slot_count=1)
        assert lineup_text(single).splitlines()[0] == "0 of 1 slot booked \u00b7 1 open"

    def test_name_escaped(self) -> None:
        from bot.ui.embeds import lineup_rows

        event = self._event(
            [make_slot(1, dj_page_id="d1", dj_name="**x** @everyone <@1>")], slot_count=1
        )
        cell = lineup_rows(event)[0][2]
        assert "@everyone" not in cell.replace("@\u200beveryone", "")
        assert "<@1>" not in cell

    def test_dj_on_two_slots(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        event = self._event(
            [
                make_slot(1, dj_page_id="d1", dj_name="Nova"),
                make_slot(3, dj_page_id="d1", dj_name="Nova"),
            ]
        )
        dj_column = next(f for f in LineupUpdateEmbed(event).fields if f.name == "DJ")
        assert dj_column.value.count("Nova") == 2

    def test_truncation_stays_within_discord_limits(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        slots = [make_slot(i, dj_page_id=f"d{i}", dj_name="n" * 100) for i in range(1, 101)]
        event = self._event(slots, slot_count=100)
        embed = LineupUpdateEmbed(event, shown=[event] * 8)
        assert len(embed) <= 6000
        assert len(embed.fields) <= 25
        assert all(len(f.value) <= 1024 for f in embed.fields)
        assert embed.description is not None and len(embed.description) <= 4096
        shown = len(next(f for f in embed.fields if f.name == "Slot").value.split("\n"))
        rows_after = sum(
            len(f.value.split("\n")) for f in embed.fields if f.name == "\u200b"
        ) // 3
        total_shown = shown + rows_after
        assert total_shown < 100
        assert embed.description.splitlines()[-1] == f"{100 - total_shown} slots not shown"

    def test_all_columns_stay_the_same_length(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        slots = [make_slot(i, dj_page_id=f"d{i}", dj_name="n" * 100) for i in range(1, 41)]
        embed = LineupUpdateEmbed(self._event(slots, slot_count=40))
        columns = [f for f in embed.fields if f.name in ("Slot", "Time", "DJ", "\u200b")]
        for start in range(0, len(columns), 3):
            lengths = {len(f.value.split("\n")) for f in columns[start : start + 3]}
            assert len(lengths) == 1

    def test_no_note_when_nothing_is_cut(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        slots = [make_slot(i, dj_page_id=f"d{i}", dj_name="n" * 100) for i in range(1, 11)]
        embed = LineupUpdateEmbed(self._event(slots, slot_count=10))
        assert "not shown" not in (embed.description or "")

    def test_truncation_singular_note(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        notes = set()
        for count in range(20, 60):
            slots = [
                make_slot(i, dj_page_id=f"d{i}", dj_name="n" * 100) for i in range(1, count + 1)
            ]
            last = (
                LineupUpdateEmbed(self._event(slots, slot_count=count))
                .description.splitlines()[-1]
            )
            if "not shown" in last:
                notes.add(last)
        assert not any(n.startswith("1 slots") for n in notes)

    def test_duplicate_rows_render_lowest_page_id(self) -> None:
        from bot.ui.embeds import lineup_rows

        event = self._event(
            [
                make_slot(1, dj_page_id="d1", dj_name="Nova", page_id="p2"),
                make_slot(1, page_id="p1"),
            ],
            slot_count=1,
        )
        assert lineup_rows(event)[0] == (
            "**Slot 1**", texts.slot_time(event, 0), "\u25ab\ufe0f *open slot*"
        )

    def test_embed_title_footer_color(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        embed = LineupUpdateEmbed(self._event([]))
        assert embed.title == "\U0001f3b6 EV-1 \u00b7 Saturday 14.11.2026"
        assert embed.footer.text == "EV-1"
        assert embed.colour is not None
        assert embed.colour.value == EmbedColors.MISSION_GOLD


class TestLineupTitle:
    def test_dated_event_id_shows_only_the_name(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        event = make_event(
            event_id="2026-11-14 Rave Night", date=datetime.date(2026, 11, 14), slot_count=1
        )
        assert LineupUpdateEmbed(event).title == "\U0001f3b6 Rave Night \u00b7 Saturday 14.11.2026"

    def test_long_name_keeps_title_within_discord_limit(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        event = make_event(event_id="x" * 500, date=datetime.date(2026, 11, 14), slot_count=1)
        title = LineupUpdateEmbed(event).title
        assert title is not None and len(title) <= 256 and title.endswith("Saturday 14.11.2026")


class TestLogEntryEmbed:
    def test_fields_and_footer(self) -> None:
        from bot.ui.embeds import EmbedColors, LogEntryEmbed

        assert EmbedColors.SUCCESS_GREEN == 0x2E7D32
        embed = LogEntryEmbed("T", "body", EmbedColors.IMPERIAL_RED, "Do it")
        assert embed.footer.text == "RaveBot"
        assert (embed.fields[0].name, embed.fields[0].value) == ("What to do", "Do it")
        assert LogEntryEmbed("T", "b", EmbedColors.IMPERIAL_RED).fields == []

    def test_long_description_is_cut_with_note(self) -> None:
        from bot.ui.embeds import EmbedColors, LogEntryEmbed

        embed = LogEntryEmbed("T", "x" * 5000, EmbedColors.IMPERIAL_RED)
        assert len(embed.description) <= 4096
        assert embed.description.endswith("(cut)")


class TestLineupLayout:
    def test_lineup_shows_meta_slots_and_notes(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed, lineup_rows, lineup_text

        e = make_event(genre="Techno", location="Club Vortex", notes="Doors 19:30")
        text = lineup_text(e)
        assert text.startswith("\U0001f3b5 Techno \u00b7 \U0001f4cd Club Vortex\n\n")
        assert text.endswith("\n\n**Notes**\nDoors 19:30")
        assert re.fullmatch(r"<t:\d+:t>", lineup_rows(e)[0][1])
        assert [f.name for f in LineupUpdateEmbed(e).fields][:3] == ["Slot", "Time", "DJ"]

    def test_without_meta_or_notes_has_nothing_extra(self) -> None:
        from bot.ui.embeds import lineup_text

        text = lineup_text(make_event())
        assert "\U0001f3b5" not in text and "\U0001f4cd" not in text and "Notes" not in text
        assert text.splitlines()[0].endswith("open")
        assert not text.endswith("\n")

    def test_only_location_set(self) -> None:
        from bot.ui.embeds import lineup_text

        assert lineup_text(make_event(location="Club")).startswith("\U0001f4cd Club\n\n")

    def test_overlong_note_is_dropped_not_cut(self) -> None:
        from bot.ui.embeds import lineup_text

        text = lineup_text(make_event(notes="x" * 5000))
        assert "Notes" not in text and len(text) <= 4096

    def test_note_kept_when_it_fits(self) -> None:
        from bot.ui.embeds import lineup_text

        assert "**Notes**" in lineup_text(make_event(slot_count=5, notes="x" * 300))

    def test_note_is_escaped(self) -> None:
        from bot.ui.embeds import lineup_text

        assert "@everyone" not in lineup_text(make_event(notes="@everyone <@1>")).replace(
            "@\u200beveryone", ""
        )


@pytest.mark.asyncio
async def test_no_button_label_contains_a_time() -> None:
    from bot.ui.views import CancellationView, SlotSelectionView

    event = make_event(slot_count=6)
    lock = asyncio.Lock()
    slot_view = SlotSelectionView(MagicMock(), event, list(range(6)), lock, 1)
    cancel_view = CancellationView(
        MagicMock(),
        [{"event": event, "slot_index": i, "slot_unix": i} for i in range(4)],
        lock,
        1,
    )
    labels = [c.label for v in (slot_view, cancel_view) for c in v.children]
    assert labels
    assert not [lbl for lbl in labels if re.search(r"\d{1,2}:\d{2}", lbl)]
    assert [c.label for c in cancel_view.children][:4] == [f"Booking {i}" for i in range(1, 5)]
