"""Pure validation helpers for the DJ onboarding and profile editing."""
from typing import Literal

MAX_NAME_LENGTH = 80
MAX_VRCHAT_LENGTH = 80
MAX_LINK_LENGTH = 500
_STREAM_HOSTS = ("twitch", "youtube", "stream.vrcdn")


def clean_text(value: str) -> str:
    """Strip and collapse whitespace runs to one space."""
    return " ".join(value.split())


def stream_link_ok(link: str) -> bool:
    lowered = link.lower()
    return any(host in lowered for host in _STREAM_HOSTS)


def normalize_link(link: str) -> str:
    """Trim; prepend https:// only when no scheme ("://") is typed."""
    stripped = link.strip()
    if "://" in stripped:
        return stripped
    return f"https://{stripped}"


def name_error(name: str) -> Literal["empty", "too_long"] | None:
    cleaned = clean_text(name)
    if not cleaned:
        return "empty"
    if len(cleaned) > MAX_NAME_LENGTH:
        return "too_long"
    return None


def names_equal(a: str, b: str) -> bool:
    return clean_text(a).casefold() == clean_text(b).casefold()


# --- The admin DJ form checks (admin page, /dj commands, DJs added in Notion) ---
# Each returns (clean value, None) or (None, error text for the organizer).


def check_name(raw: str) -> tuple[str | None, str | None]:
    problem = name_error(raw)
    if problem == "empty":
        return None, "The DJ name is required."
    if problem == "too_long":
        return None, f"The DJ name is too long (at most {MAX_NAME_LENGTH} characters)."
    return clean_text(raw), None


def check_vrchat(raw: str | None) -> tuple[str | None, str | None]:
    text = clean_text(raw or "")
    if len(text) > MAX_VRCHAT_LENGTH:
        return None, f"The VRChat name is too long (at most {MAX_VRCHAT_LENGTH} characters)."
    return (text or None), None


def check_link(raw: str, label: str) -> tuple[str | None, str | None]:
    """(clean link, None) or (None, error).

    The same links a DJ can save in the DM conversation ("Use as is"): any scheme
    such as https:// or rtmp://, followed by a host, no spaces.
    """
    link = normalize_link(raw)
    scheme, _, rest = link.partition("://")
    if not scheme.isalpha() or not rest.strip("/") or any(c.isspace() for c in link):
        return None, f"The {label} must be a link (for example https://...)."
    if len(link) > MAX_LINK_LENGTH:
        return None, f"The {label} is too long."
    return link, None


def check_optional_link(raw: str | None) -> tuple[str | None, str | None]:
    """The backup link: empty means none."""
    if not (raw or "").strip():
        return None, None
    return check_link(raw or "", "backup link")
