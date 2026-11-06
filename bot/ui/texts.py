"""Central catalog of user-facing strings and small formatters (no em dash allowed)."""
from bot.notion.models import EventInfo


def fmt_date(event: EventInfo) -> str:
    """`Saturday 14.11.2026`."""
    return event.date.strftime("%A %d.%m.%Y")


def fmt_time(event: EventInfo, slot_index: int) -> str:
    """HH:MM (UTC) of the zero-based slot_index."""
    return event.slot_start(slot_index).strftime("%H:%M")


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
    "Welcome! I'm RaveBot and I handle DJ bookings on this server.\n"
    "Join the DJ pool to get invites when a slot is open. "
    "You can also book and cancel slots yourself at any time."
)
BTN_JOIN_POOL = "Join the DJ pool"
BTN_NOT_NOW = "Not now"
ONBOARD_DECLINED = "No problem. Write me any message to open the menu and join later."
ONBOARD_DONE = (
    "You're in the DJ pool. I'll message you when a slot opens. "
    "You can book any time from the menu."
)


def link_invalid(fields: str) -> str:
    return (
        f"That link doesn't look right: {fields}. "
        'Links must start with https://. Tap "Try again" to fix it.'
    )


BTN_TRY_AGAIN = "Try again"


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
BTN_BOOK = "Book a slot"
BTN_CANCEL = "Cancel a slot"
BTN_LINEUP = "Show lineup"
BTN_INVITES_ON = "Contact me for Booking"
BTN_INVITES_OFF = "Don't automatically ask for booking"
INVITES_ON_DONE = "I'll contact you when a slot opens."
INVITES_OFF_DONE = (
    "I won't ask you for bookings automatically anymore. You can still book any time."
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
