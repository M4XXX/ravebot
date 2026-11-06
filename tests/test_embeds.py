"""Tests for bot/ui/embeds.py — EmbedColors, ErrorEmbed, AdminLogEmbed, MissionRosterEmbed."""
import datetime

from bot.notion.models import EventInfo
from bot.ui.embeds import (
    AdminLogEmbed,
    EmbedColors,
    ErrorCode,
    ErrorEmbed,
    MissionRosterEmbed,
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


class TestErrorEmbed:
    def test_color_is_imperial_red(self) -> None:
        """ErrorEmbed uses IMPERIAL_RED color."""
        embed = ErrorEmbed(ErrorCode.ERR_DM_FAILED, "DJ blocked DMs", "Logged event")
        assert embed.color.value == EmbedColors.IMPERIAL_RED

    def test_title_is_error_code_string(self) -> None:
        """ErrorEmbed title equals the error code string."""
        embed = ErrorEmbed(ErrorCode.ERR_DB_UNAVAILABLE, "DB down", "Paused scheduler")
        assert embed.title == "ERR_DB_UNAVAILABLE"

    def test_has_three_required_fields(self) -> None:
        """ErrorEmbed has Trigger Condition, Action Taken, Manual Action Required."""
        embed = ErrorEmbed(ErrorCode.ERR_NOTION_WRITE, "Write failed", "Retried 3x", "Check key")
        field_names = [f.name for f in embed.fields]
        assert "Trigger Condition" in field_names
        assert "Action Taken" in field_names
        assert "Manual Action Required" in field_names

    def test_default_manual_action(self) -> None:
        """Manual action defaults to 'No manual action required.' when omitted."""
        embed = ErrorEmbed(ErrorCode.ERR_BUTTON_TIMEOUT, "Timeout", "Moved to next DJ")
        values = {f.name: f.value for f in embed.fields}
        assert values["Manual Action Required"] == "No manual action required."

    def test_custom_manual_action(self) -> None:
        """Custom manual action is stored correctly."""
        embed = ErrorEmbed(
            ErrorCode.ERR_OUTREACH_EXHAUSTED, "All DJs contacted", "Alerted admin", "Book manually"
        )
        values = {f.name: f.value for f in embed.fields}
        assert values["Manual Action Required"] == "Book manually"

    def test_has_footer(self) -> None:
        """ErrorEmbed footer says 'RaveBot'."""
        embed = ErrorEmbed(ErrorCode.ERR_SLOT_CONFLICT, "Slot taken", "Showed updated list")
        assert embed.footer.text == "RaveBot"

    def test_has_timestamp(self) -> None:
        """ErrorEmbed has a timestamp set."""
        embed = ErrorEmbed(ErrorCode.ERR_DM_FAILED, "DM blocked", "Logged")
        assert embed.timestamp is not None


class TestAdminLogEmbed:
    def test_color_is_nebula_white(self) -> None:
        """AdminLogEmbed uses NEBULA_WHITE color."""
        embed = AdminLogEmbed("DJ Felix", "Booking", "Slot secured")
        assert embed.color.value == EmbedColors.NEBULA_WHITE

    def test_title_format(self) -> None:
        """AdminLogEmbed title follows '📋 DM Log · {dj_name} → {action_type}' format."""
        embed = AdminLogEmbed("DJ Felix", "Onboarding", "Completed")
        assert embed.title == "📋 DM Log · DJ Felix → Onboarding"

    def test_has_dj_action_outcome_fields(self) -> None:
        """AdminLogEmbed always has DJ, Action, and Outcome fields."""
        embed = AdminLogEmbed("DJ Mars", "Cancellation", "Slot freed")
        field_names = [f.name for f in embed.fields]
        assert "DJ" in field_names
        assert "Action" in field_names
        assert "Outcome" in field_names

    def test_no_event_field_when_omitted(self) -> None:
        """Event field is absent when not provided."""
        embed = AdminLogEmbed("DJ Mars", "Outreach", "Accepted")
        field_names = [f.name for f in embed.fields]
        assert "Event" not in field_names

    def test_event_field_present_when_provided(self) -> None:
        """Event field is included when provided."""
        embed = AdminLogEmbed("DJ Mars", "Outreach", "Accepted", event="Rave 2026-06-01")
        field_names = [f.name for f in embed.fields]
        assert "Event" in field_names

    def test_field_values_correct(self) -> None:
        """DJ, Action, Outcome field values match constructor args."""
        embed = AdminLogEmbed("DJ Venus", "Booking", "Confirmed slot at 9PM")
        values = {f.name: f.value for f in embed.fields}
        assert values["DJ"] == "DJ Venus"
        assert values["Action"] == "Booking"
        assert values["Outcome"] == "Confirmed slot at 9PM"

    def test_has_footer(self) -> None:
        """AdminLogEmbed footer says 'RaveBot'."""
        embed = AdminLogEmbed("DJ Felix", "Booking", "Done")
        assert embed.footer.text == "RaveBot"

    def test_has_timestamp(self) -> None:
        """AdminLogEmbed has a timestamp set."""
        embed = AdminLogEmbed("DJ Felix", "Booking", "Done")
        assert embed.timestamp is not None


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


class TestMissionRosterEmbed:
    def test_mission_roster_embed_color(self) -> None:
        """MissionRosterEmbed uses MISSION_GOLD color."""
        embed = MissionRosterEmbed([_booked_event()])
        assert embed.color.value == EmbedColors.MISSION_GOLD

    def test_mission_roster_embed_title(self) -> None:
        """MissionRosterEmbed title is '🌌 Mission Roster'."""
        embed = MissionRosterEmbed([_booked_event()])
        assert embed.title == "🌌 Mission Roster"

    def test_mission_roster_embed_footer(self) -> None:
        """MissionRosterEmbed footer says 'RaveBot'."""
        embed = MissionRosterEmbed([_booked_event()])
        assert embed.footer.text == "RaveBot"

    def test_mission_roster_embed_empty_events(self) -> None:
        """MissionRosterEmbed with no events shows 'No upcoming events scheduled.'."""
        embed = MissionRosterEmbed([])
        assert embed.description == "No upcoming events scheduled."
        assert len(embed.fields) == 0

    def test_mission_roster_embed_has_event_field(self) -> None:
        """MissionRosterEmbed adds one field per event."""
        embed = MissionRosterEmbed([_booked_event("Event A"), _booked_event("Event B")])
        assert len(embed.fields) == 2

    def test_mission_roster_embed_field_name_is_event_id(self) -> None:
        """Each field name is the event title (event_id)."""
        embed = MissionRosterEmbed([_booked_event("Friday Night Rave")])
        assert embed.fields[0].name == "Friday Night Rave"

    def test_mission_roster_embed_field_value_contains_slot_summary(self) -> None:
        """Field value shows confirmed/open slot counts from slot rows."""
        embed = MissionRosterEmbed([_booked_event(slots=6, djs=2)])
        assert "2 confirmed" in embed.fields[0].value
        assert "4 open" in embed.fields[0].value

    def test_mission_roster_embed_dj_with_two_slots_counts_two(self) -> None:
        """A DJ holding two slots counts as two booked."""
        event = make_event(
            slot_count=6,
            slots=[
                make_slot(1, dj_page_id="dj-a", dj_name="A"),
                make_slot(2, dj_page_id="dj-a", dj_name="A"),
                make_slot(3),
            ],
        )
        embed = MissionRosterEmbed([event])
        assert "2 confirmed" in embed.fields[0].value
        assert "4 open" in embed.fields[0].value

    def test_mission_roster_embed_event_time(self) -> None:
        """Field value shows the first slot start as a Discord timestamp."""
        event = _booked_event()
        embed = MissionRosterEmbed([event])
        assert f"<t:{int(event.slot_start(0).timestamp())}:f>" in embed.fields[0].value

    def test_mission_roster_embed_timestamp_set(self) -> None:
        """MissionRosterEmbed has a timestamp set."""
        embed = MissionRosterEmbed([_booked_event()])
        assert embed.timestamp is not None

    def test_mission_roster_embed_zero_slots(self) -> None:
        """slot_count=0 gives 0 confirmed and 0 open."""
        embed = MissionRosterEmbed([_booked_event(slots=0, djs=0)])
        assert "0 confirmed" in embed.fields[0].value
        assert "0 open" in embed.fields[0].value


class TestEmbedHardening:
    def test_safe_dj_name_none_is_guest(self) -> None:
        from bot.ui.embeds import _safe_dj_name

        assert _safe_dj_name(None) == "Guest DJ"

    def test_roster_caps_fields_at_25(self) -> None:
        events = [
            make_event(
                event_id=f"E{i}",
                slot_count=2,
                slots=[make_slot(1, dj_page_id="d", dj_name="D")],
            )
            for i in range(40)
        ]
        embed = MissionRosterEmbed(events)
        assert len(embed.fields) == 25
        assert embed.fields[24].name == "More events"
        assert "16 more events" in embed.fields[24].value


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


def test_mission_roster_field_name_capped_at_256_characters() -> None:
    long_id = "2026-12-31 " + "x" * 400
    embed = MissionRosterEmbed([_booked_event(long_id), _booked_event("Short")])
    assert len(embed.fields[0].name) == 256
    assert embed.fields[0].name.startswith("2026-12-31 xxx")
    assert embed.fields[1].name == "Short"


def test_mission_roster_field_name_of_exactly_256_characters_is_kept() -> None:
    exact = "y" * 256
    embed = MissionRosterEmbed([_booked_event(exact)])
    assert embed.fields[0].name == exact


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
        assert embed.description.splitlines()[-1].endswith(" slots not shown")

    def test_embed_title_footer_color(self) -> None:
        from bot.ui.embeds import LineupUpdateEmbed

        embed = LineupUpdateEmbed(self._event([]))
        assert embed.title == "Lineup Update"
        assert embed.footer.text == "EV-1"
        assert embed.colour is not None
        assert embed.colour.value == EmbedColors.MISSION_GOLD
