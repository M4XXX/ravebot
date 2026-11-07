"""Central catalog of user-facing strings and small formatters (no em dash allowed)."""
import disnake

from bot.notion.models import EventInfo


def fmt_date(event: EventInfo) -> str:
    """`Saturday 14.11.2026`."""
    return event.date.strftime("%A %d.%m.%Y")


def fmt_time(event: EventInfo, slot_index: int) -> str:
    """Local HH:MM of the zero-based slot_index."""
    return event.slot_start(slot_index).strftime("%H:%M")


def fmt_slot_when(event: EventInfo, slot_index: int) -> str:
    """`Saturday 14.11.2026, 19:00` (local time of the slot)."""
    start = event.slot_start(slot_index)
    return f"{start.strftime('%A %d.%m.%Y')}, {start.strftime('%H:%M')}"


def slot_phrase(event: EventInfo, slot_index: int) -> str:
    """`slot 2 at 19:00` for the zero-based slot_index."""
    return f"slot {slot_index + 1} at {fmt_time(event, slot_index)}"


GENERIC_ERROR = (
    "Something went wrong. Please try again in a moment. "
    "If it keeps happening, tell an organizer."
)
FLOW_OPEN = "You already have a booking open. Finish or dismiss it first."
NO_PROFILE = (
    "You haven't set up your DJ profile yet. Ask an organizer to give you the DJ role again."
)
WELCOME = (
    "**Welcome to the DJ team**\n"
    "I'm RaveBot and I handle DJ bookings on this server.\n\n"
    "Join the **Active DJ Booking Group** and I will contact you automatically "
    "when a slot is open. If you don't join, you can still book and cancel slots "
    "yourself at any time."
)
BTN_JOIN_POOL = "Yes, I want to be booked"
BTN_NOT_NOW = "Not now"
BTN_SKIP = "Skip"
BTN_CANCEL_SETUP = "Cancel"
BTN_USE_AS_IS = "Use it as it is"
BTN_LOOKS_GOOD = "Looks good"
BTN_START_OVER = "Start over"
BTN_REMOVE = "Remove"
BTN_MY_PROFILE = "My DJ profile"
BTN_CHANGE_NAME = "Change DJ name"
BTN_CHANGE_VRCHAT = "Change VRChat name"
BTN_CHANGE_LOGO = "Change logo"
BTN_CHANGE_LINK = "Change stream link"
BTN_CHANGE_ALT_LINK = "Change backup link"
ONBOARD_DECLINED = "No problem. Write me any message to open the menu and join later."
ONBOARD_DONE = (
    "**You're in the Active DJ Booking Group**\n"
    "I will message you when a slot opens. You can book any time from the menu, "
    "and change your profile under **My DJ profile**."
)
ONBOARD_CANCELLED = "Setup cancelled. Write me any message to open the menu."
STALE_BUTTON = (
    "This message is out of date. Use the latest message from me, or press Cancel there."
)

Q_NAME = (
    "**Step 1 of 5: DJ name**\n"
    "Which DJ name should be shown on posters and in the lineup? "
    "Just write it as a message."
)
Q_VRCHAT = "**Step 2 of 5: VRChat name**\nWhat is your VRChat username?"
Q_LOGO = (
    "**Step 3 of 5: Logo**\n"
    "Send me one image (PNG or JPEG, up to 5 MB). It will be used to advertise you."
)
Q_LINK = (
    "**Step 4 of 5: Stream link**\n"
    "Where do you stream? Send the link (Twitch, YouTube or stream.vrcdn)."
)
Q_ALT_LINK = (
    "**Step 5 of 5: Backup stream link**\n"
    "Do you have a second link in case the first one fails?"
)
NAME_EMPTY = "Please send a name."
NAME_TOO_LONG = "That is too long. Please send a name with at most 80 characters."
LOGO_MANY = "Please send just one image, the one you want used to advertise you."
LOGO_NEEDS_FILE = "Please send the image as a file, not as a link."
LOGO_BAD = "That file isn't a usable image. Please send a PNG or JPEG under 5 MB."
VRCHAT_EMPTY_OPTIONAL = "Please send a name, or press Skip."
LINK_EMPTY = "Please send a link."
LINK_TOO_LONG = "That link is too long. Please send a shorter link."
_LINK_ECHO_MAX = 200
LINK_EMPTY_OPTIONAL = "Please send a link, or press Skip."
ALREADY_DJ = "You already have a DJ profile. Open it from the menu under My DJ profile."
EDIT_CANCELLED = "Nothing changed."
SUMMARY_TITLE = "**Your DJ profile**"
OVERVIEW_TITLE = "**My DJ profile**"
EDIT_LOGO_PROMPT = "Send me the new logo as one image (PNG or JPEG, up to 5 MB)."
FIELD_LABELS = {
    "name": "Name",
    "vrchat": "VRChat name",
    "logo": "Logo",
    "link": "Stream link",
    "alt_link": "Backup link",
}
_EDIT_SOURCES = {
    "name": Q_NAME,
    "vrchat": Q_VRCHAT,
    "link": Q_LINK,
    "alt_link": Q_ALT_LINK,
}


def name_taken(name: str) -> str:
    return f'There is already a DJ called "{name}". Please send a different name.'


def link_check_failed(link: str) -> str:
    if len(link) > _LINK_ECHO_MAX:
        link = link[:_LINK_ECHO_MAX] + "..."
    return (
        f"That link doesn't look like a Twitch, YouTube or stream.vrcdn link: {link}\n"
        "Send a corrected link, or keep it if it is right."
    )


def edit_saved(field_label: str) -> str:
    return f"{field_label} updated."


def edit_prompt(field: str) -> str:
    """Question for editing one field: the Q_* text without its step prefix."""
    if field == "logo":
        return EDIT_LOGO_PROMPT
    return "**" + _EDIT_SOURCES[field].split(": ", 1)[1]


def profile_text(
    *,
    dj_name: str,
    vrchat_name: str | None,
    logo: str | None,
    logo_attached: bool,
    stream_link: str | None,
    alt_link: str | None,
    show_group: bool | None = None,
) -> str:
    if logo_attached:
        logo_line = "shown below"
    else:
        logo_line = logo or "none"
    lines = [
        f"DJ name: {dj_name}",
        f"VRChat name: {vrchat_name or 'none'}",
        f"Logo: {logo_line}",
        f"Stream link: {stream_link or 'none'}",
        f"Backup link: {alt_link or 'none'}",
    ]
    if show_group is not None:
        lines.append(f"Active DJ Booking Group: {'yes' if show_group else 'no'}")
    return "\n".join(lines)


def invite_opener(event_id: str) -> str:
    return f"Hi! There are open slots at **{event_id}**. Want one?"


BTN_PICK_SLOT = "Pick a Slot"
BTN_NOT_THIS_TIME = "Not this time"
INVITE_DECLINED = "No problem. I'll ask again when another slot opens."
INVITE_FILLED = "Those slots were just taken. Nothing is open right now."
PICK_SLOT = "Which slot do you want?"
PICK_SLOT_TRUNCATED = "Showing the first 24 open slots."
BTN_NEVER_MIND = "Never mind"


def slot_button(event: EventInfo, slot_index: int) -> str:
    return f"Slot {slot_index + 1} · {fmt_time(event, slot_index)}"


PICK_EVENT = "**Which event do you want to play?**"


def event_line(index: int, event: EventInfo, open_count: int) -> str:
    open_text = "1 slot open" if open_count == 1 else f"{open_count} slots open"
    return f"{index}. {event.event_id} · {fmt_date(event)} · {open_text}"


CONFIRM_TITLE = "Confirm your booking"
BTN_CONFIRM = "Confirm"
BTN_BACK = "Back"
BOOKING_ABORTED = "No problem. Nothing was booked."
BOOKED_TITLE = "You're booked"


def booking_body(event: EventInfo, slot_index: int) -> str:
    return f"{event.event_id}\n{fmt_date(event)}, {slot_phrase(event, slot_index)}"


BOOKED_REMINDER_NOTE = "I'll remind you the day before."
SLOT_TAKEN = "That slot was just taken by someone else. These slots are still open:"
EVENT_FULL = "All slots of this event are now booked. I'll message you if one opens up."
ALL_BOOKED = "All slots are booked. I'll message you if one opens up."
NO_EVENTS = "There are no upcoming events yet."
CANCEL_WHICH = "Which booking do you want to cancel?"


def cancel_confirm(event: EventInfo, slot_index: int) -> str:
    return (
        f"Cancel slot {slot_index + 1} ({fmt_time(event, slot_index)}) at {event.event_id}?"
    )


BTN_CANCEL_YES = "Yes, cancel"
BTN_CANCEL_KEEP = "Keep booking"
CANCELLED = "Your booking is cancelled. The slot is open again."
CANCEL_KEPT = "Nothing changed. You're still booked."
NO_BOOKINGS = "You have no upcoming bookings."
MENU = "I work with buttons. What do you want to do?"
CONFIG_SHOW_HINT = "To change a setting, run /setup again."
SETUP_NOT_FINISHED = "The setup is not finished yet. Run /setup to finish it."
BTN_BOOK = "Book a slot"
BTN_CANCEL = "Cancel a slot"
BTN_LINEUP = "Show lineup"
BTN_INVITES_ON = "Contact me for Booking"
BTN_INVITES_OFF = "Don't automatically ask for booking"
# Group button of the "My DJ profile" overview.
BTN_LEAVE_GROUP = "Leave the booking group"
BTN_JOIN_GROUP = "Join the Active DJ Booking Group"
INVITES_ON_DONE = "You're in the Active DJ Booking Group. I'll contact you when a slot opens."
INVITES_OFF_DONE = (
    "You left the Active DJ Booking Group. I won't ask you for bookings "
    "automatically anymore. You can still book any time."
)


def reminder(event: EventInfo, slot_index: int) -> str:
    return (
        "**You're on tomorrow!** \U0001f3a7\n"
        f"{event.event_id}\n"
        f"{fmt_date(event)}, {slot_phrase(event, slot_index)}\n"
        "Check your gear and your stream link. Have a great set!"
    )


def dm_booked(event: EventInfo, slot_index: int) -> str:
    return f"An organizer booked you: {event.event_id}, {slot_phrase(event, slot_index)}."


def dm_cancelled(event: EventInfo, slot_index: int) -> str:
    return (
        f"An organizer cancelled your booking: "
        f"{event.event_id}, {slot_phrase(event, slot_index)}."
    )


def dm_added(event: EventInfo, slot_index: int) -> str:
    return (
        f"You are in the lineup: {event.event_id}, "
        f"{slot_phrase(event, slot_index)} (added by an organizer)."
    )


def dm_removed(event: EventInfo, slot_index: int) -> str:
    return (
        f"You were removed from {event.event_id}, "
        f"{slot_phrase(event, slot_index)} (by an organizer)."
    )


BTN_BOOK_POST = "Book a slot"
POST_DM_SENT = "I sent you a DM."
POST_DM_BLOCKED = (
    "I can't send you a DM. Please allow DMs from server members "
    "(Discord Settings, Privacy & Safety) and press the button again."
)
NO_LINEUP = "There is no upcoming event yet."


# --- Log channel entries (names passed in are already escaped by the caller) ---

LOG_BOOKED_TITLE = "Booked"
LOG_CANCELLED_TITLE = "Cancelled"
LOG_ORGANIZER_TITLE = "Changed by an organizer"
LOG_NOTION_TITLE = "Changed in Notion"
LOG_FULLY_BOOKED_TITLE = "Event fully booked"
LOG_NEW_DJ_TITLE = "New DJ"
LOG_TODO_FIELD = "What to do"
LOG_NO_MANUAL_ACTION = "No manual action required."
LOG_FAILED_TITLE = "Something failed"
LOG_NEW_DATABASES_TITLE = "New Notion databases created"
NOTION_RETRY_TODO = "Check the Slots rows in Notion. The next sync retries."
DJ_CHANNEL_TODO = "Run /setup again to set the DJ channel."


def dm_failed_todo(name: str) -> str:
    """Todo line for every blocked-DM entry; the name is escaped."""
    return (
        f"Tell {escape_text(name)} yourself. They need to allow DMs from server members "
        "(Discord Settings, Privacy & Safety)."
    )


def escape_text(text: str) -> str:
    """Literal rendering of foreign text: markdown, mentions and <...> tokens neutralised."""
    escaped = disnake.utils.escape_mentions(disnake.utils.escape_markdown(text))
    return escaped.replace("<", "<\u200b")


def _ev(event: EventInfo) -> str:
    return f"**{escape_text(event.event_id)}**"


def log_booked_text(dj: str, event: EventInfo, slot_index: int, via: str) -> str:
    where = "via DM" if via == "DM" else "added in Notion"
    return f"{dj} booked {_ev(event)}, {slot_phrase(event, slot_index)} ({where})"


def log_cancelled_text(dj: str, event: EventInfo, slot_index: int) -> str:
    return f"{dj} cancelled {_ev(event)}, {slot_phrase(event, slot_index)} (via DM)"


def log_organizer_text(
    organizer: str, action: str, dj: str, event: EventInfo, slot_index: int
) -> str:
    when = slot_phrase(event, slot_index)
    if action == "booked":
        return f"{organizer} booked {dj} into {_ev(event)}, {when}"
    return f"{organizer} removed {dj} from {_ev(event)}, {when}"


def log_notion_text(dj: str, event: EventInfo, slot_index: int) -> str:
    return f"{dj} was removed from {_ev(event)}, {slot_phrase(event, slot_index)}"


def log_fully_booked_text(event: EventInfo) -> str:
    return f"{_ev(event)}: all {event.slot_count} slots are booked."


def log_new_dj_text(name: str, vrchat_name: str | None) -> str:
    if vrchat_name:
        return f"{name} (VRChat: {vrchat_name}) joined the DJ pool."
    return f"{name} joined the DJ pool."


def log_message_title(display_name: str, dj_name: str | None) -> str:
    if dj_name and dj_name != display_name:
        return f"Message from {display_name} (DJ {dj_name})"
    return f"Message from {display_name}"


def log_message_text(text: str, attachment_names: list[str]) -> str:
    lines = [text] if text else []
    lines.extend(f"(attachment: {name})" for name in attachment_names)
    return "\n".join(lines)

LOG_REPLY_ORGANIZERS_ONLY = "Only organizers can reply."
LOG_REPLY_NO_USER = "I cannot find the user for this message."
LOG_REPLY_SENT = "Sent."
LOG_REPLY_FAILED = (
    "I could not send the message. The user may have DMs from server members turned off."
)


# --- Setup wizard ---
SETUP_ONLY_ORGANIZERS = "Only organizers can change the setup."
SETUP_ALREADY_ACTIVE = (
    "The bot is already active. You can go through the steps again to change settings: "
    "nothing changes until you press Save."
)
SETUP_ALREADY_OPEN = "The setup is already open here: {url}"
SETUP_ALREADY_DONE = "This setup was already finished."
SETUP_OUTDATED = "This setup message is outdated. Run /setup to open the current one."
SETUP_NOT_A_RERUN = "This is the first setup. Use Activate on the last step."
SETUP_CHANGE_BUTTON = "Change settings"
SETUP_ERROR = "Something went wrong during the setup. Press the button again."
ADMIN_SERVER_ONLY = "Use this command on the server."
SETUP_NO_PERMISSION = "You don't have permission to use this command."
SETUP_SAVED = "Settings saved. To change a setting later, run /setup again."
SETUP_MISSING_VALUES = "Some steps are not finished yet. Go back and choose a channel."
SETUP_CHECK_OK = "✓ "
SETUP_CHECK_FAIL = "✗ "


def setup_welcome(total: int, checks: list[tuple[bool, str]], rerun: bool = False) -> str:
    intro = (
        f"I will guide you through {total} short steps. The bot stays active: "
        "nothing changes until you press **Save** at the end."
        if rerun
        else f"I will guide you through {total} short steps. Until you press **Activate** "
        "at the end, I stay idle: no posts, no invites, no events."
    )
    lines = [
        f"**Step 1 of {total}: Welcome and checks**",
        "**RaveBot setup**",
        intro,
        "",
    ]
    lines += [f"{SETUP_CHECK_OK if ok else SETUP_CHECK_FAIL}{text}" for ok, text in checks]
    return "\n".join(lines)


def setup_channel_step(
    number: int, total: int, title: str, question: str, chosen: str | None, check: str | None
) -> str:
    lines = [f"**Step {number} of {total}: {title}**", question, ""]
    if chosen is None:
        lines.append("No channel selected yet.")
    else:
        lines.append(f"Selected: {chosen}")
    if check:
        lines.append(check)
    return "\n".join(lines)


SETUP_DJ_CHANNEL_QUESTION = 'Where should I post the Lineup Update and the "Book a slot" button?'
SETUP_LOG_CHANNEL_QUESTION = (
    "Where should I post bookings, new DJs, problems and messages from DJs? "
    "Only organizers should see this channel."
)


def setup_channel_ok(channel_name: str) -> str:
    return f"{SETUP_CHECK_OK}I can send messages and embeds in #{channel_name}"


def setup_channel_missing(channel_name: str, missing: list[str]) -> str:
    return (
        f"{SETUP_CHECK_FAIL}I am missing the permission {', '.join(missing)} "
        f"in #{channel_name}. Fix it in the channel settings and select the channel again."
    )


SETUP_CHANNEL_GONE = f"{SETUP_CHECK_FAIL}I can't see that channel. Select another one."


def setup_roles_step(
    number: int, total: int, dj: str | None, invites: str | None, check: str | None
) -> str:
    lines = [
        f"**Step {number} of {total}: Roles**",
        "**DJ role:** when someone gets this role, I send them the welcome DM.",
        "**Invites role:** I give this role to DJs who want automatic booking invites.",
        "",
        f"DJ role: {dj or 'not selected'}",
        f"Invites role: {invites or 'not selected'}",
    ]
    if check:
        lines.append(check)
    return "\n".join(lines)


SETUP_ROLES_OK = f"{SETUP_CHECK_OK}I can manage these roles"
SETUP_ROLE_MISSING_PERMISSION = (
    f"{SETUP_CHECK_FAIL}I am missing the permission Manage Roles. "
    "Give it to my role and select the roles again."
)
SETUP_ROLE_GONE = f"{SETUP_CHECK_FAIL}A selected role no longer exists. Select it again."
SETUP_CREATE_NEEDS_PERMISSION = (
    "I can't create the role: I am missing the permission Manage Roles."
)
SETUP_CREATE_FAILED = "I could not create the role. Check my permissions and try again."
INVITES_ROLE_NAME = "Active Booking"
SETUP_ROLES_SAME = f"{SETUP_CHECK_FAIL}Choose two different roles for DJs and invites."
SETUP_ROLE_EVERYONE = f"{SETUP_CHECK_FAIL}@everyone can't be used here. Choose another role."


def setup_role_managed(role_name: str) -> str:
    return (
        f"{SETUP_CHECK_FAIL}@{role_name} belongs to an integration and can't be "
        "given to members. Choose another role."
    )



def setup_role_too_high(role_name: str) -> str:
    return (
        f"{SETUP_CHECK_FAIL}I can't manage the role @{role_name}: "
        "move my role above it in the server settings."
    )


def setup_events_step(number: int, total: int, v: dict) -> str:
    return "\n".join(
        [
            f"**Step {number} of {total}: Events**",
            "New events are created automatically with these settings:",
            f"Slots per event: {v['slots_per_event']}",
            f"Start time ({v['zone_abbr']}): {v['default_start_time']}",
            f"Slot length: {v['slot_duration_minutes']} minutes",
            f"One event every: {v['event_interval_days']} days",
            f"Future events kept ready: {v['event_lookahead_count']}",
            f"Time zone: {v['timezone']}",
            "",
            f"Start times are local time in {v['timezone']} (currently {v['zone_abbr']}): "
            f"{v['default_start_time']} stays {v['default_start_time']} all year. "
            "When an event is done or canceled, I create the next one so the number "
            f"stays at {v['event_lookahead_count']}.",
        ]
    )


def setup_invites_step(number: int, total: int, v: dict) -> str:
    return "\n".join(
        [
            f"**Step {number} of {total}: Automatic invites**",
            "I can message DJs when an event has open slots.",
            f"Automatic invites: {'on' if v['outreach_enabled'] else 'off'}",
            f"Start asking: {v['outreach_days_before']} days before an event",
            "Wait between two invites to the same DJ: "
            f"{v['outreach_cooldown_hours']} hours",
        ]
    )


def setup_invalid(field: str, rule: str) -> str:
    return f"{field}: {rule}"


SETUP_ZONE_CHANGED_WARNING = (
    "Warning: the time zone changed, so the real times of existing events change."
)
SETUP_INVITES_ROLE_WARNING = (
    "Warning: the invites role changed, so members are not moved to the new role."
)


def setup_summary(lines: list[str], rerun: bool, total: int = 6) -> str:
    head = "Ready to save" if rerun else "Ready to activate"
    return "\n".join([f"**Step {total} of {total}: {head}**", *lines])


def setup_active_message(created: int, posted: bool, channel: str | None) -> str:
    parts = []
    if created > 0:
        parts.append(f"I created your first {created} events in Notion")
    if posted:
        where = f" in {channel}" if channel else ""
        parts.append(f"posted the Lineup Update{where}")
    body = " and ".join(parts)
    lines = ["**RaveBot is active**"]
    if body:
        lines.append(f"{body}. To change a setting later, run /setup again.")
    else:
        lines.append("To change a setting later, run /setup again.")
    return "\n".join(lines)

SETUP_NOT_IN_GUILD = "Please use this command in a server channel."
