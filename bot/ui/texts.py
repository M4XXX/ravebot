"""Central catalog of user-facing strings and small formatters (no em dash allowed)."""
from collections.abc import Sequence

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


FLOW_STALE = "This menu is out of date. Open the menu again to start over."
FLOW_BUSY = "I'm already working on it. One moment, please."
FLOW_EXPIRED = "This menu has expired. Open the menu again to start over."


def flow_expired(content: str | None) -> str:
    """The text of an expired flow message: its old text plus the expiry note."""
    return f"{content}\n\n{FLOW_EXPIRED}" if content else FLOW_EXPIRED


GENERIC_ERROR = (
    "Something went wrong. Please try again in a moment. "
    "If it keeps happening, tell an organizer."
)
FLOW_OPEN = (
    "You already have a booking open. Finish it or press Never mind. "
    "It closes by itself after 15 minutes."
)
NO_PROFILE = (
    "You haven't set up your DJ profile yet. Ask an organizer to give you the DJ role again."
)
ONBOARD_BEFORE_BOOKING = (
    "You need a DJ profile before you can book a slot. Let's set it up now. "
    "When it is saved, press **Book a slot** again."
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


PICK_EVENT = "Which event do you want to play?"


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


def cancel_more(total: int) -> str:
    return (
        f"You have {total} bookings; your next 4 are shown. "
        "Cancel one of them to see the others, or ask an organizer."
    )


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
LOG_NOTION_APPLIED_TITLE = "Applied from Notion"
LOG_FULLY_BOOKED_TITLE = "Event fully booked"
LOG_NEW_DJ_TITLE = "New DJ"
LOG_ADMIN_ADDED_TITLE = "Admin added"
LOG_DJ_LIST_TITLE = "DJ list changed"
LOG_TODO_FIELD = "What to do"
LOG_NO_MANUAL_ACTION = "No manual action required."
LOG_FAILED_TITLE = "Something failed"
LOG_NOTION_MISSING_TITLE = "Notion databases not found"
LOG_NOTION_MISSING_TODO = (
    "If the integration lost access, share the Notion page with it again and restart the "
    "bot. If the databases were deleted on purpose, open the admin page and press "
    "Create new databases."
)
LOG_NEW_DATABASES_TITLE = "New Notion databases created"
MAINTENANCE = "The bot is restoring its data. Please try again in a few minutes."
LOG_UPGRADE_DONE_TITLE = "Data imported from Notion"
LOG_UPGRADE_FAILED_TITLE = "Import from Notion failed"
LOG_UPGRADE_FAILED_TODO = (
    "Fix these rows in Notion, then restart the bot. The import runs again on the next "
    "start. Until then booking and edits stay paused."
)
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


def log_admin_added_text(name: str, user_id: int) -> str:
    return f"{name} (ID {user_id}) was added as an admin."


def log_dj_list_text(admin: str, action: str, dj_name: str) -> str:
    return f"{admin} {action} the DJ {dj_name}."


def log_notion_applied_text(lines: list[str]) -> str:
    """Hand edits from Notion the bot applied: up to 10 lines, then "and N more"."""
    text = "\n".join(lines[:10])
    if len(lines) > 10:
        text += f"\nand {len(lines) - 10} more"
    return text[:3500]


# --- Notion sync cycle (summaries are plain text; the sync cycle escapes before posting) ---

LOG_SYNC_CONFLICT_TITLE = "Changed in the bot and in Notion"
LOG_SYNC_WARNING_TITLE = "Notion edit not applied"
LOG_SYNC_EXPIRED_TITLE = "Postgres kept after 24 h"
LOG_SYNC_READ_FAILED_TITLE = "Notion could not be read"
SYNC_CONFLICT_TODO = (
    "Keep the bot's version or Notion's: choose on the admin page. Until then this record "
    "is not synced. Without a decision the bot's version wins after 24 h."
)
SYNC_RESYNC_TODO = (
    "Fix it in Notion (the next poll tries again), or choose resync on the admin page to "
    "write the bot's version to Notion."
)
SYNC_DELETED_TODO = (
    "To bring the Notion page back, choose resync on the admin page. To remove the record "
    "for good, remove it in the bot."
)
SYNC_DUPLICATE_TODO = "Delete the extra page in Notion."
SYNC_MASS_TODO = (
    "Check that the integration still has access to the Notion page, then choose resync "
    "on the admin page to write everything to Notion again."
)
SYNC_READ_FAILED_TODO = (
    "Check that Notion is reachable and the integration still has access. The next poll "
    "tries again."
)
SYNC_ISSUE_NOT_OPEN = "This conflict or warning is no longer open."
SYNC_WRONG_CHOICE = "This choice does not fit this entry."
SYNC_KEPT_POSTGRES = "The bot's version is kept and written to Notion."
SYNC_KEPT_NOTION = "Notion's version is applied."
SYNC_RESYNCED = "The bot's version is written to Notion again."


def sync_conflict_summary(label: str) -> str:
    return f"{label} was changed in the bot and in Notion."


def sync_conflict_text(summary: str, diffs: list[str]) -> str:
    return "\n".join([summary, *diffs])[:3500]


def sync_diff_line(field: str, bot_value: str, notion_value: str) -> str:
    return f"{field}: bot {bot_value}, Notion {notion_value}"


def sync_deleted_summary(label: str) -> str:
    return f"The Notion page of {label} was deleted. The bot keeps its record."


def sync_key_changed_summary(label: str, field: str) -> str:
    return f"The {field} of {label} was changed in Notion. The bot keeps its record."


def sync_duplicate_summary(label: str) -> str:
    return f"A Notion page for {label} repeats a record the bot already has. It was not applied."


def sync_mass_summary(what: str) -> str:
    return f"Many {what} are missing in Notion. The bot changed nothing."


def sync_unreadable_summary(label: str) -> str:
    return (
        f"The Notion page of {label} has an empty required field or points to a page the "
        "bot does not know. The bot ignores the page until it is fixed."
    )


def sync_rejected_summary(label: str, reasons: list[str], *, new: bool) -> str:
    what = f"A new {label} in Notion" if new else f"A Notion edit of {label}"
    return f"{what} was not applied: {' '.join(reasons)}"


def sync_expired_text(summary: str) -> str:
    return (
        f"{summary} Nobody chose a version within 24 h, so the bot's version is written "
        "to Notion again."
    )


def sync_read_failed_text(error: str) -> str:
    return f"The Notion poll could not read the databases ({error}). Nothing was changed."


def sync_notion_rejected(reasons: list[str]) -> str:
    return f"Notion's version cannot be applied: {' '.join(reasons)} The conflict stays open."


def sync_resync_all_done(count: int) -> str:
    return f"{count} records are queued for Notion."


def log_upgrade_done_text(events: int, slots: int, djs: int, notes: list[str]) -> str:
    text = (
        "The bot now keeps events, slots and DJs in its own database; Notion is a backup. "
        f"Imported {events} events, {slots} slots and {djs} DJs."
    )
    if notes:
        shown = "; ".join(notes[:10])
        more = f" (and {len(notes) - 10} more)" if len(notes) > 10 else ""
        text += f" Skipped or changed: {shown}{more}."
    return text[:3500]


def log_upgrade_failed_text(problems: list[str]) -> str:
    shown = "; ".join(problems[:10])
    more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
    return (
        "Nothing was imported from Notion, so booking and edits are paused. "
        f"Problems: {shown}{more}."
    )[:3500]


def log_notion_missing_text(detail: str) -> str:
    return (
        "The bot cannot find its Notion databases, so booking and the lineup are paused. "
        f"Details: {detail[:500]}"
    )


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


# Title and question of each value step, keyed by config key.
setup_value_texts: dict[str, tuple[str, str]] = {
    "slots_per_event": ("Slots per event", "How many DJ slots should each event have?"),
    "default_start_time": ("Start time", "When should an event start?"),
    "slot_duration_minutes": ("Slot length", "How long is one DJ slot?"),
    "event_interval_days": (
        "Event interval",
        "How many days should be between two scheduled events?",
    ),
    "first_event_date": ("First event", "When should the first scheduled event take place?"),
    "event_lookahead_count": (
        "Events kept ready",
        "How many future events should always be ready?",
    ),
    "timezone": ("Time zone", "Which time zone do start times use?"),
}


CREATE_EVENT_NOT_MARKED = (
    " I could not record it as a manual event, so the schedule may count it as a scheduled one."
)


def setup_value_step(
    number: int, total: int, title: str, question: str, current: str, note: str | None = None
) -> str:
    lines = [
        f"**Step {number} of {total}: {title}**",
        question,
        f"Current: {current}",
        "Pick a value, or press Other to type your own.",
    ]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def setup_schedule_step(number: int, total: int, enabled: bool) -> str:
    lines = [
        f"**Step {number} of {total}: Scheduled events**",
        "I can create events on a schedule, so new ones are always ready.",
        f"Scheduled events: {'on' if enabled else 'off'}",
    ]
    if enabled:
        lines.append("The next steps set the interval, the first date and how many stay ready.")
    else:
        lines.append(
            "Off: I create no events by myself. Create them with /create event. "
            "Slots, start time, slot length and time zone still apply to those events."
        )
    return "\n".join(lines)


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


# --- /cancel event (the event is "canceled", as the Notion status spells it) ---

LOG_EVENT_CANCELED_TITLE = "Event canceled"
EVENT_CANCELED_ANYWAY = "The event was canceled anyway."
EVENT_CANCEL_CLEANUP_TODO = (
    "The event is canceled, so these slots cannot be booked. Nothing else needs to be done."
)
EVENT_CANCEL_POST_FAILED = "The cancel notice could not be posted in the DJ channel."
EVENT_CANCEL_KEPT = "Nothing was canceled."
EVENT_NOT_CANCELABLE = (
    "That event was not found, is in the past, or is already done or canceled."
)
EVENT_CANCEL_SAVE_FAILED = (
    "The database could not be updated. The event was not canceled and nobody was told."
)
EVENT_WAS_CANCELED = "This event was canceled. Open the menu again to pick another event."

# Lines shown in the /cancel event confirm message before "and N more"
_CONFIRM_MAX_LINES = 15
# Discord allows 2000 characters per message
_CONFIRM_MAX_CHARS = 1900


def _slots_phrase(event: EventInfo, slot_indexes: Sequence[int]) -> str:
    return ", ".join(slot_phrase(event, index) for index in slot_indexes)


def dm_event_canceled(event: EventInfo, slot_indexes: Sequence[int]) -> str:
    return (
        f"The event {escape_text(event.event_id)} was canceled by an organizer. "
        f"Your booking ({_slots_phrase(event, slot_indexes)}) is canceled too."
    )


def dj_line(name: str, event: EventInfo, slot_indexes: Sequence[int]) -> str:
    """One list line for a booked DJ; the name is escaped."""
    return f"- {escape_text(name)}, {_slots_phrase(event, slot_indexes)}"


def log_event_canceled_text(admin: str, event: EventInfo, dj_lines: Sequence[str]) -> str:
    """dj_lines come from dj_line(); admin is already escaped by the log service."""
    head = f"{admin} canceled {_ev(event)}."
    if not dj_lines:
        return f"{head}\nNo DJ was booked."
    return f"{head}\nBooked DJs:\n" + "\n".join(dj_lines)


def dj_channel_event_canceled(event: EventInfo) -> str:
    return f"{_ev(event)} on {fmt_date(event)} is canceled."


def event_canceled_dm_failed(name: str, event: EventInfo) -> str:
    return (
        f"The notice that {_ev(event)} was canceled could not be delivered to "
        f"{escape_text(name)} because their DMs are closed or the bot cannot reach them."
    )


def event_canceled_no_discord(name: str, event: EventInfo) -> str:
    return (
        f"{escape_text(name)} has no Discord account, so the bot could not tell them "
        f"that {_ev(event)} was canceled."
    )


def tell_canceled_todo(name: str) -> str:
    return f"Tell {escape_text(name)} yourself that the event is canceled."


def event_cancel_cleanup_failed(event: EventInfo, slot_indexes: Sequence[int]) -> str:
    numbers = ", ".join(str(index + 1) for index in slot_indexes)
    return (
        f"{_ev(event)} was canceled, but these slots could not be emptied: {numbers}."
    )


def _capped(items: Sequence[str]) -> list[str]:
    """At most _CONFIRM_MAX_LINES items, then "and N more"."""
    shown = list(items[:_CONFIRM_MAX_LINES])
    if len(items) > _CONFIRM_MAX_LINES:
        shown.append(f"and {len(items) - _CONFIRM_MAX_LINES} more")
    return shown


def cancel_event_confirm(
    event: EventInfo, dj_lines: Sequence[str], by_hand: Sequence[str]
) -> str:
    """The question of the /cancel event confirm step.

    dj_lines (from dj_line()) are the DJs told by DM; by_hand are raw names of DJs
    without a Discord account.
    """
    parts = [f"Cancel the event {_ev(event)}?"]
    if not dj_lines and not by_hand:
        parts.append("No DJ is booked.")
    if dj_lines:
        parts.append("These DJs are booked and will be told by DM:")
        parts.extend(_capped(dj_lines))
    if by_hand:
        names = ", ".join(_capped([escape_text(name) for name in by_hand]))
        parts.append(f"Without a Discord account, tell them yourself: {names}")
    text = "\n".join(parts)
    if len(text) > _CONFIRM_MAX_CHARS:
        text = text[:_CONFIRM_MAX_CHARS] + "\n..."
    return text


def event_cancel_done(
    event: EventInfo,
    notified: Sequence[str],
    by_hand: Sequence[str],
    failed_slots: Sequence[int],
) -> str:
    """The answer to the admin after /cancel event ran."""
    text = f"Canceled {escape_text(event.event_id)}."
    if notified:
        count = len(notified)
        text += f" {count} DJ{'s' if count != 1 else ''} told by DM."
    if by_hand:
        names = ", ".join(_capped([escape_text(name) for name in by_hand]))
        text += f" Tell these DJs yourself: {names}."
    if failed_slots:
        text += " Some slots could not be emptied, see the log channel."
    return text
