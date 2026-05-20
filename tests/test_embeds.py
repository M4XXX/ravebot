"""Tests for bot/ui/embeds.py — EmbedColors, ErrorEmbed, AdminLogEmbed, MissionRosterEmbed."""
from bot.ui.embeds import AdminLogEmbed, EmbedColors, ErrorCode, ErrorEmbed, MissionOverviewEmbed, MissionRosterEmbed


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


class TestMissionRosterEmbed:
    def _make_event(
        self,
        name: str = "Friday Night Rave",
        date_str: str = "2026-06-13T20:00:00+00:00",
        slots: int = 6,
        djs: int = 2,
    ) -> dict:
        return {
            "id": "page-uuid",
            "last_edited_time": "2026-06-01T00:00:00.000Z",
            "properties": {
                "Name": {"title": [{"plain_text": name}]},
                "Date": {"date": {"start": date_str}},
                "Slots": {"number": slots},
                "DJs": {"relation": [{"id": f"dj-{i}"} for i in range(djs)]},
            },
        }

    def test_mission_roster_embed_color(self) -> None:
        """MissionRosterEmbed uses MISSION_GOLD color."""
        embed = MissionRosterEmbed([self._make_event()])
        assert embed.color.value == EmbedColors.MISSION_GOLD

    def test_mission_roster_embed_title(self) -> None:
        """MissionRosterEmbed title is '🌌 Mission Roster'."""
        embed = MissionRosterEmbed([self._make_event()])
        assert embed.title == "🌌 Mission Roster"

    def test_mission_roster_embed_footer(self) -> None:
        """MissionRosterEmbed footer says 'RaveBot'."""
        embed = MissionRosterEmbed([self._make_event()])
        assert embed.footer.text == "RaveBot"

    def test_mission_roster_embed_empty_events(self) -> None:
        """MissionRosterEmbed with no events shows 'No upcoming events scheduled.'."""
        embed = MissionRosterEmbed([])
        assert embed.description == "No upcoming events scheduled."
        assert len(embed.fields) == 0

    def test_mission_roster_embed_has_event_field(self) -> None:
        """MissionRosterEmbed adds one field per event."""
        embed = MissionRosterEmbed([self._make_event("Event A"), self._make_event("Event B")])
        assert len(embed.fields) == 2

    def test_mission_roster_embed_field_name_contains_event_name(self) -> None:
        """Each field name contains the event name."""
        embed = MissionRosterEmbed([self._make_event("Friday Night Rave")])
        assert "Friday Night Rave" in embed.fields[0].name

    def test_mission_roster_embed_field_value_contains_slot_summary(self) -> None:
        """Field value shows confirmed/open slot counts."""
        embed = MissionRosterEmbed([self._make_event(slots=6, djs=2)])
        assert "2 confirmed" in embed.fields[0].value
        assert "4 open" in embed.fields[0].value

    def test_mission_roster_embed_timestamp_set(self) -> None:
        """MissionRosterEmbed has a timestamp set."""
        embed = MissionRosterEmbed([self._make_event()])
        assert embed.timestamp is not None

    def test_mission_roster_embed_default_slots_when_missing(self) -> None:
        """Defaults to 6 slots when Slots property is missing."""
        event = self._make_event(djs=1)
        del event["properties"]["Slots"]
        embed = MissionRosterEmbed([event])
        assert "5 open" in embed.fields[0].value

    def test_mission_roster_embed_handles_missing_date(self) -> None:
        """Shows 'TBD' when Date property is missing."""
        event = self._make_event()
        del event["properties"]["Date"]
        embed = MissionRosterEmbed([event])
        assert "TBD" in embed.fields[0].value

    def test_mission_roster_embed_handles_missing_name(self) -> None:
        """Shows 'Unnamed Event' when Name property is missing."""
        event = self._make_event()
        del event["properties"]["Name"]
        embed = MissionRosterEmbed([event])
        assert "Unnamed Event" in embed.fields[0].name

    def test_mission_roster_embed_zero_slots_not_coerced(self) -> None:
        """Slots=0 is not coerced to 6 — uses 0 as total."""
        embed = MissionRosterEmbed([self._make_event(slots=0, djs=0)])
        assert "0 confirmed" in embed.fields[0].value
        assert "0 open" in embed.fields[0].value


class TestMissionOverviewEmbed:
    def _make_event(
        self,
        name: str = "Friday Night Rave",
        date_str: str = "2026-06-13T20:00:00+00:00",
        slots: int = 3,
        djs: int = 1,
    ) -> dict:
        return {
            "id": "page-uuid",
            "last_edited_time": "2026-06-01T00:00:00.000Z",
            "properties": {
                "Name": {"title": [{"plain_text": name}]},
                "Date": {"date": {"start": date_str}},
                "Slots": {"number": slots},
                "DJs": {"relation": [{"id": f"dj-{i}"} for i in range(djs)]},
            },
        }

    def test_mission_overview_embed_color(self) -> None:
        """MissionOverviewEmbed uses DEEP_SPACE_BLUE color."""
        embed = MissionOverviewEmbed(self._make_event())
        assert embed.color.value == EmbedColors.DEEP_SPACE_BLUE

    def test_mission_overview_embed_title(self) -> None:
        """MissionOverviewEmbed title is '🛰️ {event_name}'."""
        embed = MissionOverviewEmbed(self._make_event(name="Friday Night Rave"))
        assert embed.title == "🛰️ Friday Night Rave"

    def test_mission_overview_embed_footer(self) -> None:
        """MissionOverviewEmbed footer says 'RaveBot'."""
        embed = MissionOverviewEmbed(self._make_event())
        assert embed.footer.text == "RaveBot"

    def test_mission_overview_embed_timestamp(self) -> None:
        """MissionOverviewEmbed has a timestamp set."""
        embed = MissionOverviewEmbed(self._make_event())
        assert embed.timestamp is not None

    def test_mission_overview_embed_description_partial(self) -> None:
        """Open slots description shows 'confirmed' and 'open' counts."""
        embed = MissionOverviewEmbed(self._make_event(slots=3, djs=1))
        assert "confirmed" in embed.description
        assert "open" in embed.description

    def test_mission_overview_embed_description_has_event_date(self) -> None:
        """Description includes event date as Discord absolute timestamp (AC1)."""
        embed = MissionOverviewEmbed(self._make_event())
        assert "<t:" in embed.description
        assert ":f>" in embed.description

    def test_mission_overview_embed_description_all_filled(self) -> None:
        """All slots booked → description contains 'All slots filled' with event date."""
        embed = MissionOverviewEmbed(self._make_event(slots=3, djs=3))
        assert "All slots filled" in embed.description

    def test_mission_overview_embed_slot_count_fields(self) -> None:
        """Number of embed fields equals number of slots."""
        embed = MissionOverviewEmbed(self._make_event(slots=4, djs=0))
        assert len(embed.fields) == 4

    def test_mission_overview_embed_open_slot_shows_open(self) -> None:
        """Slot beyond booked_count shows 'Open' in field value."""
        embed = MissionOverviewEmbed(self._make_event(slots=3, djs=1))
        # Slot index 1 and 2 are open (0-indexed, booked_count=1)
        assert "Open" in embed.fields[1].value
        assert "Open" in embed.fields[2].value

    def test_mission_overview_embed_booked_slot_shows_confirmed(self) -> None:
        """Booked slot (index < booked_count) shows 'Confirmed' in field value."""
        embed = MissionOverviewEmbed(self._make_event(slots=3, djs=2))
        assert "Confirmed" in embed.fields[0].value
        assert "Confirmed" in embed.fields[1].value

    def test_mission_overview_embed_slot_has_discord_timestamp(self) -> None:
        """Each slot value contains a Discord relative timestamp when date is present."""
        embed = MissionOverviewEmbed(self._make_event(slots=2, djs=0))
        for field in embed.fields:
            assert "<t:" in field.value
            assert ":R>" in field.value

    def test_mission_overview_embed_handles_missing_date(self) -> None:
        """Missing Date property: no crash, no '<t:' timestamps (shows TBD)."""
        event = self._make_event(slots=2, djs=0)
        del event["properties"]["Date"]
        embed = MissionOverviewEmbed(event)
        for field in embed.fields:
            assert "<t:" not in field.value

    def test_mission_overview_embed_handles_missing_name(self) -> None:
        """Missing Name property → title contains 'Unknown Event'."""
        event = self._make_event()
        del event["properties"]["Name"]
        embed = MissionOverviewEmbed(event)
        assert "Unknown Event" in embed.title

    def test_mission_overview_embed_default_slots(self) -> None:
        """Missing Slots property → defaults to 6 fields."""
        event = self._make_event()
        del event["properties"]["Slots"]
        embed = MissionOverviewEmbed(event)
        assert len(embed.fields) == 6
