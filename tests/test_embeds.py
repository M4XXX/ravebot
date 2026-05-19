"""Tests for bot/ui/embeds.py — EmbedColors, ErrorEmbed, AdminLogEmbed."""
from bot.ui.embeds import AdminLogEmbed, EmbedColors, ErrorCode, ErrorEmbed


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
