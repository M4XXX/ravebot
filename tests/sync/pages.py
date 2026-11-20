"""Raw Notion page builders (the JSON shape NotionClient.read_all returns)."""
from typing import Any

from bot.notion.client import NotionSnapshot


def _text(value: str | None) -> list[dict[str, str]]:
    return [{"plain_text": value}] if value else []


def event_page(
    page_id: str,
    title: str | None,
    date: str = "2026-12-01",
    *,
    start: str = "20:00",
    slots: int | None = 3,
    length: int = 60,
    status: str | None = None,
    genre: str | None = None,
    bot_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "ID": {"title": _text(title)},
            "Date": {"date": {"start": date} if date else None},
            "Start": {"rich_text": _text(start)},
            "Slots": {"number": slots},
            "Slot length": {"number": length},
            "Status": {"select": {"name": status} if status else None},
            "Genre": {"rich_text": _text(genre)},
            "Notes": {"rich_text": []},
            "Bot ID": {"rich_text": _text(bot_id)},
        },
    }


def slot_page(
    page_id: str, event: str | None, number: int | None, *djs: str
) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Event": {"relation": [{"id": event}] if event else []},
            "Slot number": {"number": number},
            "DJ": {"relation": [{"id": d} for d in djs]},
        },
    }


def dj_page(
    page_id: str,
    name: str | None,
    discord_id: str | int | None = None,
    *,
    vrchat: str | None = None,
    link: str | None = None,
    logo: str | None = None,
    logo_kind: str = "file",
) -> dict[str, Any]:
    files = [{"name": "logo.png", "type": logo_kind, logo_kind: {"url": logo}}] if logo else []
    return {
        "id": page_id,
        "properties": {
            "Logo": {"files": files},
            "DJ name": {"title": _text(name)},
            "Discord ID": {"rich_text": _text(str(discord_id) if discord_id is not None else None)},
            "VRChat name": {"rich_text": _text(vrchat)},
            "Link": {"url": link},
            "Alternative link": {"url": None},
        },
    }


def snapshot(events=(), slots=(), djs=()) -> NotionSnapshot:  # noqa: ANN001
    return NotionSnapshot(events=list(events), slots=list(slots), djs=list(djs), botdata=[])
