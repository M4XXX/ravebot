"""Pure validation helpers for the DJ onboarding and profile editing."""
from typing import Literal

MAX_NAME_LENGTH = 80
MAX_VRCHAT_LENGTH = 80
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
