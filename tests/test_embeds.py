"""Tests for bot/ui/embeds.py — EmbedColors, ErrorCode."""
import datetime

from bot.notion.models import EventInfo
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
        assert embed.description == "Test Rave\nSaturday 13.06.2026, slot 1 at 20:00"


class TestBookedEmbed:
    def test_uses_mission_gold(self) -> None:
        from bot.ui.embeds import BookedEmbed
        embed = BookedEmbed(make_event(), 0)
        assert embed.color.value == EmbedColors.MISSION_GOLD

    def test_title_and_body(self) -> None:
        from bot.ui.embeds import BookedEmbed
        embed = BookedEmbed(make_event(event_id="Test Rave"), 1)
        assert embed.title == "You're booked"
        assert embed.description == (
            "Test Rave\nSaturday 13.06.2026, slot 2 at 21:00\nI'll remind you the day before."
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

    def test_lineup_text_exact(self) -> None:
        from bot.ui.embeds import lineup_text

        event = self._event(
            [make_slot(1), make_slot(2, dj_page_id="d1", dj_name="Nova")]
        )
        assert lineup_text(event) == (
            "Event Saturday 14.11.2026\n"
            "Slot 1  18:00  OPEN SLOT\n"
            "Slot 2  19:00  Nova\n"
            "Slot 3  20:00  OPEN SLOT"
        )

    def test_name_escaped(self) -> None:
        from bot.ui.embeds import lineup_text

        event = self._event(
            [make_slot(1, dj_page_id="d1", dj_name="**x** @everyone <@1>")], slot_count=1
        )
        text = lineup_text(event)
        assert "@everyone" not in text.replace("@​everyone", "")
        assert "<@1>" not in text

    def test_dj_on_two_slots(self) -> None:
        from bot.ui.embeds import lineup_text

        event = self._event(
            [
                make_slot(1, dj_page_id="d1", dj_name="Nova"),
                make_slot(3, dj_page_id="d1", dj_name="Nova"),
            ]
        )
        assert lineup_text(event).count("Nova") == 2

    def test_truncation(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        slots = [make_slot(i, dj_page_id=f"d{i}", dj_name="n" * 100) for i in range(1, 101)]
        event = self._event(slots, slot_count=100)
        embed = LineupUpdateEmbed(event)
        assert embed.description is not None
        assert len(embed.description) <= 4096
        lines = embed.description.splitlines()
        shown = len(lines) - 2  # event line and note
        assert lines[-1] == f"{100 - shown} slots not shown"

    def test_no_note_when_nothing_is_cut(self) -> None:
        from bot.ui.embeds import lineup_text

        slots = [make_slot(i, dj_page_id=f"d{i}", dj_name="n" * 100) for i in range(1, 11)]
        assert "not shown" not in lineup_text(self._event(slots, slot_count=10))

    def test_truncation_singular_note(self) -> None:
        from bot.ui.embeds import lineup_text

        notes = set()
        for count in range(20, 60):
            slots = [make_slot(i, dj_page_id=f"d{i}", dj_name="n" * 100) for i in range(1, count + 1)]
            last = lineup_text(self._event(slots, slot_count=count)).splitlines()[-1]
            if "not shown" in last:
                notes.add(last)
        assert "1 slot not shown" in notes
        assert "1 slots not shown" not in notes

    def test_duplicate_rows_render_lowest_page_id(self) -> None:
        from bot.ui.embeds import lineup_text

        event = self._event(
            [
                make_slot(1, dj_page_id="d1", dj_name="Nova", page_id="p2"),
                make_slot(1, page_id="p1"),
            ],
            slot_count=1,
        )
        assert lineup_text(event).splitlines()[1] == "Slot 1  18:00  OPEN SLOT"

    def test_embed_title_footer_color(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        embed = LineupUpdateEmbed(self._event([]))
        assert embed.title == "Lineup Update"
        assert embed.footer.text == "EV-1"
        assert embed.colour is not None
        assert embed.colour.value == EmbedColors.MISSION_GOLD


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
