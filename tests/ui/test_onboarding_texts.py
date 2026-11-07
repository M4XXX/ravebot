"""Exact catalog strings for the DJ onboarding rework."""
import pytest

from bot.ui import texts


def test_welcome_and_join_button() -> None:
    assert "Active DJ Booking Group" in texts.WELCOME
    assert texts.BTN_JOIN_POOL == "Yes, I want to be booked"
    assert texts.BTN_NOT_NOW == "Not now"


def test_button_labels() -> None:
    assert texts.BTN_SKIP == "Skip"
    assert texts.BTN_CANCEL_SETUP == "Cancel"
    assert texts.BTN_USE_AS_IS == "Use it as it is"
    assert texts.BTN_LOOKS_GOOD == "Looks good"
    assert texts.BTN_START_OVER == "Start over"
    assert texts.BTN_REMOVE == "Remove"
    assert texts.BTN_MY_PROFILE == "My DJ profile"


def test_questions_have_step_prefix() -> None:
    for n, q in enumerate(
        (texts.Q_NAME, texts.Q_VRCHAT, texts.Q_LOGO, texts.Q_LINK, texts.Q_ALT_LINK), 1
    ):
        assert q.startswith(f"**Step {n} of 5: ")


def test_name_and_link_messages() -> None:
    assert texts.name_taken("Neon") == (
        'There is already a DJ called "Neon". Please send a different name.'
    )
    assert texts.NAME_EMPTY == "Please send a name."
    assert "at most 80" in texts.NAME_TOO_LONG
    msg = texts.link_check_failed("example.com")
    assert msg.startswith(
        "That link doesn't look like a Twitch, YouTube or stream.vrcdn link: example.com\n"
    )


def test_misc_constants() -> None:
    assert texts.SUMMARY_TITLE == "**Your DJ profile**"
    assert texts.OVERVIEW_TITLE == "**My DJ profile**"
    assert texts.ONBOARD_DONE.startswith("**You're in the Active DJ Booking Group**")
    assert texts.ONBOARD_CANCELLED.startswith("Setup cancelled.")
    assert texts.STALE_BUTTON.startswith("This message is out of date.")
    assert texts.INVITES_ON_DONE.startswith("You're in the Active DJ Booking Group.")
    assert "Active DJ Booking Group" in texts.INVITES_OFF_DONE
    assert texts.INVITES_ROLE_NAME == "Active Booking"
    assert texts.link_invalid and texts.BTN_TRY_AGAIN == "Try again"


def test_edit_helpers() -> None:
    assert texts.edit_saved("Name") == "Name updated."
    assert texts.edit_prompt("name") == (
        "**DJ name**\nWhich DJ name should be shown on posters and in the lineup? "
        "Just write it as a message."
    )
    assert texts.edit_prompt("logo") == texts.EDIT_LOGO_PROMPT
    assert "Step" not in texts.edit_prompt("alt_link")
    assert texts.FIELD_LABELS == {
        "name": "Name",
        "vrchat": "VRChat name",
        "logo": "Logo",
        "link": "Stream link",
        "alt_link": "Backup link",
    }


def test_profile_text_none_and_logo_variants() -> None:
    base = dict(dj_name="Neon", vrchat_name="", stream_link="https://twitch.tv/x", alt_link="")
    t = texts.profile_text(**base, logo=None, logo_attached=False)
    assert t == (
        "DJ name: Neon\nVRChat name: none\nLogo: none\n"
        "Stream link: https://twitch.tv/x\nBackup link: none"
    )
    assert "Logo: shown below" in texts.profile_text(**base, logo=None, logo_attached=True)
    assert "Logo: https://x/y.png" in texts.profile_text(
        **base, logo="https://x/y.png", logo_attached=False
    )


@pytest.mark.parametrize(
    "flag,line",
    [(True, "Active DJ Booking Group: yes"), (False, "Active DJ Booking Group: no")],
)
def test_profile_text_group_line(flag: bool, line: str) -> None:
    t = texts.profile_text(
        dj_name="N", vrchat_name="v", logo=None, logo_attached=False,
        stream_link="s", alt_link="a", show_group=flag,
    )
    assert t.endswith(line)
