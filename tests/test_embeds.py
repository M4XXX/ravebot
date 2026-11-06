"""Tests for bot/ui/embeds.py — EmbedColors, ErrorEmbed, AdminLogEmbed, MissionRosterEmbed."""
import datetime

from bot.notion.models import EventInfo
from bot.ui.embeds import (
    AdminLogEmbed,
    EmbedColors,
    ErrorCode,
    ErrorEmbed,
    MissionOverviewEmbed,
    MissionRosterEmbed,
    ProactiveOutreachEmbed,
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


class TestMissionOverviewEmbed:
    def test_mission_overview_embed_color(self) -> None:
        """MissionOverviewEmbed uses DEEP_SPACE_BLUE color."""
        embed = MissionOverviewEmbed(_booked_event(slots=3, djs=1))
        assert embed.color.value == EmbedColors.DEEP_SPACE_BLUE

    def test_mission_overview_embed_title(self) -> None:
        """MissionOverviewEmbed title is '🛰️ {event_id}'."""
        embed = MissionOverviewEmbed(_booked_event(name="Friday Night Rave"))
        assert embed.title == "\U0001f6f0\ufe0f Friday Night Rave"

    def test_mission_overview_embed_footer(self) -> None:
        """MissionOverviewEmbed footer says 'RaveBot'."""
        embed = MissionOverviewEmbed(_booked_event())
        assert embed.footer.text == "RaveBot"

    def test_mission_overview_embed_timestamp_set(self) -> None:
        """MissionOverviewEmbed has a timestamp set."""
        embed = MissionOverviewEmbed(_booked_event())
        assert embed.timestamp is not None

    def test_mission_overview_embed_description_partial(self) -> None:
        """Open slots description shows 'confirmed' and 'open' counts."""
        embed = MissionOverviewEmbed(_booked_event(slots=3, djs=1))
        assert "1/3 confirmed" in embed.description
        assert "2 open" in embed.description

    def test_mission_overview_embed_description_has_event_date(self) -> None:
        """Description includes event date as Discord absolute timestamp."""
        event = _booked_event()
        embed = MissionOverviewEmbed(event)
        assert f"<t:{int(event.slot_start(0).timestamp())}:f>" in embed.description

    def test_mission_overview_embed_description_all_filled(self) -> None:
        """All slots booked → description contains 'All slots filled'."""
        embed = MissionOverviewEmbed(_booked_event(slots=3, djs=3))
        assert "All slots filled" in embed.description

    def test_mission_overview_embed_one_line_per_slot(self) -> None:
        """One field per slot 1..slot_count with slot time and DJ name or Open."""
        event = make_event(
            slot_count=3,
            slots=[make_slot(1, dj_page_id="dj-1", dj_name="DJ Alpha"), make_slot(3)],
        )
        embed = MissionOverviewEmbed(event)
        assert len(embed.fields) == 3
        assert [f.name for f in embed.fields] == ["Slot 1", "Slot 2", "Slot 3"]
        for i, field in enumerate(embed.fields):
            assert f"<t:{int(event.slot_start(i).timestamp())}:R>" in field.value
        assert "DJ Alpha" in embed.fields[0].value
        assert "Open" in embed.fields[1].value
        assert "Open" in embed.fields[2].value

    def test_mission_overview_embed_unknown_dj_name_shows_guest(self) -> None:
        """A slot with a DJ but no resolvable name shows 'Guest DJ'."""
        event = make_event(slot_count=2, slots=[make_slot(1, dj_page_id="dj-x", dj_name=None)])
        embed = MissionOverviewEmbed(event)
        assert "Guest DJ" in embed.fields[0].value
        assert "Open" in embed.fields[1].value

    def test_mission_overview_embed_slot_times_follow_slot_minutes(self) -> None:
        """Slot times use the event's slot length."""
        event = make_event(slot_count=2, slot_minutes=30)
        embed = MissionOverviewEmbed(event)
        first = int(event.slot_start(0).timestamp())
        assert f"<t:{first + 1800}:R>" in embed.fields[1].value


class TestOverviewHardening:
    NASTY = "**bold** `code` @everyone <@123456789> <t:1:f> <#5>"

    def test_dj_name_rendered_literally(self) -> None:
        event = make_event(
            slot_count=1, slots=[make_slot(1, dj_page_id="d", dj_name=self.NASTY)]
        )
        value = MissionOverviewEmbed(event).fields[0].value
        assert "\\*\\*bold\\*\\*" in value
        assert "\\`code\\`" in value
        assert "@everyone" not in value
        assert "@\u200beveryone" in value
        name_part = value.split(" · ")[0]
        assert "<@" not in name_part and "<t:" not in name_part and "<#" not in name_part
        assert "<\u200b@123456789>" in value
        assert "<\u200bt:1:f>" in value

    def test_long_dj_name_truncated(self) -> None:
        event = make_event(
            slot_count=1, slots=[make_slot(1, dj_page_id="d", dj_name="a" * 300)]
        )
        value = MissionOverviewEmbed(event).fields[0].value
        name = value.split(" · ")[0]
        assert len(name) <= 101
        assert name.endswith("\u2026")

    def test_safe_dj_name_none_is_guest(self) -> None:
        from bot.ui.embeds import _safe_dj_name

        assert _safe_dj_name(None) == "Guest DJ"

    def test_overview_caps_fields_at_25(self) -> None:
        event = make_event(
            slot_count=30,
            slots=[make_slot(i, dj_page_id=f"d{i}", dj_name=f"DJ{i}") for i in (1, 2, 28)],
        )
        embed = MissionOverviewEmbed(event)
        assert len(embed.fields) == 25
        assert embed.fields[23].name == "Slot 24"
        assert embed.fields[24].name == "More slots"
        assert "6 more slots not shown" in embed.fields[24].value
        assert "5 open" in embed.fields[24].value
        assert "3/30 confirmed" in embed.description
        assert "27 open" in embed.description

    def test_overview_25_slots_all_shown(self) -> None:
        embed = MissionOverviewEmbed(make_event(slot_count=25))
        assert len(embed.fields) == 25
        assert embed.fields[24].name == "Slot 25"

    def test_overview_zero_slots(self) -> None:
        embed = MissionOverviewEmbed(make_event(slot_count=0))
        assert "All slots filled" not in embed.description
        assert "No slots." in embed.description

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


class TestProactiveOutreachEmbed:
    def test_fields_from_event_info(self) -> None:
        """Name, date and open-slot count come from the typed event."""
        event = make_event(
            event_id="Rave 1",
            slot_count=4,
            slots=[
                make_slot(1, dj_page_id="dj-a", dj_name="A"),
                make_slot(2, dj_page_id="dj-a", dj_name="A"),
            ],
        )
        embed = ProactiveOutreachEmbed(event)
        values = {f.name: f.value for f in embed.fields}
        assert values["Event"] == "Rave 1"
        assert values["Date"] == f"<t:{int(event.slot_start(0).timestamp())}:f>"
        assert values["Open Slots"] == "2 of 4"


class TestBookingConfirmEmbed:
    def test_uses_mission_gold(self) -> None:
        """BookingConfirmEmbed color is MISSION_GOLD — never inline hex."""
        from bot.ui.embeds import BookingConfirmEmbed
        embed = BookingConfirmEmbed(event_name="Friday Night Rave", slot_unix=1751400000)
        assert embed.color.value == EmbedColors.MISSION_GOLD

    def test_title_confirm_your_booking(self) -> None:
        """Title is 'Confirm Your Booking'."""
        from bot.ui.embeds import BookingConfirmEmbed
        embed = BookingConfirmEmbed(event_name="Test Rave", slot_unix=1751400000)
        assert embed.title == "Confirm Your Booking"

    def test_has_event_and_slot_fields(self) -> None:
        """BookingConfirmEmbed has Event and Slot fields."""
        from bot.ui.embeds import BookingConfirmEmbed
        embed = BookingConfirmEmbed(event_name="Test Rave", slot_unix=1751400000)
        field_names = [f.name for f in embed.fields]
        assert "Event" in field_names
        assert "Slot" in field_names

    def test_slot_unix_zero_shows_tbd(self) -> None:
        """slot_unix=0 (parse failure fallback) renders as TBD."""
        from bot.ui.embeds import BookingConfirmEmbed
        embed = BookingConfirmEmbed(event_name="Test Rave", slot_unix=0)
        values = {f.name: f.value for f in embed.fields}
        assert values["Slot"] == "TBD"


class TestBookingConfirmedEmbed:
    def test_uses_mission_gold(self) -> None:
        """BookingConfirmedEmbed color is MISSION_GOLD — never inline hex."""
        from bot.ui.embeds import BookingConfirmedEmbed
        embed = BookingConfirmedEmbed(event_name="Friday Night Rave", slot_unix=1751400000)
        assert embed.color.value == EmbedColors.MISSION_GOLD

    def test_title_slot_secured(self) -> None:
        """Title is 'Slot Secured' per UX-DR5."""
        from bot.ui.embeds import BookingConfirmedEmbed
        embed = BookingConfirmedEmbed(event_name="Test Rave", slot_unix=1751400000)
        assert embed.title == "Slot Secured"

    def test_footer_includes_event_name(self) -> None:
        """Footer text includes the event name per UX-DR5."""
        from bot.ui.embeds import BookingConfirmedEmbed
        embed = BookingConfirmedEmbed(event_name="Friday Night Rave", slot_unix=1751400000)
        assert "Friday Night Rave" in embed.footer.text

    def test_has_event_slot_headsup_fields(self) -> None:
        """BookingConfirmedEmbed has Event, Your Slot, and Heads Up fields."""
        from bot.ui.embeds import BookingConfirmedEmbed
        embed = BookingConfirmedEmbed(event_name="Test Rave", slot_unix=1751400000)
        field_names = [f.name for f in embed.fields]
        assert "Event" in field_names
        assert "Your Slot" in field_names
        assert "Heads Up" in field_names


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
