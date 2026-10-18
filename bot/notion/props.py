"""Read values from Notion page objects without depending on property names.

The title column is "Name" in the bot's own database layout, but organizers name it freely
(e.g. "Event"); Notion marks it by type, so it is found by type here.
"""
import re
from typing import Any


def title_text(page: dict) -> str:
    """Plain text of the page's title property.

    Raises KeyError when the page has no title property and IndexError when the title is
    empty — the same errors the old ["Name"]["title"][0] lookups raised, so callers keep
    their fallbacks.
    """
    props = page["properties"]
    for prop in props.values():
        if isinstance(prop, dict) and (prop.get("type") == "title" or "title" in prop):
            return prop["title"][0]["plain_text"]
    raise KeyError("title")


def normalized(name: str) -> str:
    """Property name compared without whitespace: '21:00 - 22:00' == '21:00-22:00'."""
    return re.sub(r"\s+", "", name)


def find_property(page_or_schema: dict, name: str) -> tuple[str, dict] | None:
    """(real name, property) of the property called `name`, ignoring whitespace."""
    props = page_or_schema.get("properties", {})
    if name in props:
        return name, props[name]
    wanted = normalized(name)
    for key, prop in props.items():
        if normalized(key) == wanted:
            return key, prop
    return None


def plain_text(prop: dict | None) -> str:
    """Joined plain text of a rich_text (or title) property value, stripped; '' if empty."""
    if not isinstance(prop, dict):
        return ""
    parts: Any = prop.get("rich_text", prop.get("title")) or []
    return "".join(p.get("plain_text", "") for p in parts if isinstance(p, dict)).strip()


def status_name(page: dict, name: str = "Status") -> str | None:
    """Name of the page's status (or select) value, or None when unset or missing."""
    found = find_property(page, name)
    if found is None:
        return None
    prop = found[1]
    value = prop.get("status") or prop.get("select")
    return value.get("name") if isinstance(value, dict) else None
