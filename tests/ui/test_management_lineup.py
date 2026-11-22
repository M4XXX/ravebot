"""Tests for bot/ui/management_lineup.py: channel embeds and DM payloads."""
import datetime

from bot.notion.models import EventInfo, SlotInfo
from bot.ui.management_lineup import (
    DmPayload,
    build_channel_messages,
    build_dm_payloads,
    content_hash,
    lineup_state,
)


def _slot(n, dj=None, discord_id=None):
    return SlotInfo(
        page_id=f"s{n}",
        number=n,
        dj_page_id=f"p{n}" if dj else None,
        dj_name=dj,
        discord_id=discord_id,
    )


def _event(slots, count=None, status="Planned", **kw):
    return EventInfo(
        page_id="ev1",
        event_id="Rave 1",
        date=datetime.date(2026, 12, 5),
        status=status,
        genre=kw.get("genre", "Techno"),
        notes=kw.get("notes", "Bring water"),
        slot_count=count if count is not None else len(slots),
        start=datetime.time(20, 0),
        slot_minutes=60,
        slots=tuple(slots),
        location=kw.get("location", "Club"),
    )


def _dj(uid, name, primary=None, backup=None, vrc="vrc", logo=None):
    return {
        "discord_user_id": uid,
        "dj_name": name,
        "vrchat_name": vrc,
        "stream_url_primary": primary,
        "stream_url_backup": backup,
        "logo_url": logo,
        "has_logo": logo is not None,
    }


def _text(messages):
    parts = []
    for msg in messages:
        for e in msg:
            parts += [e.title or "", e.description or ""]
            parts += [f.name + f.value for f in e.fields]
    return "\n".join(parts)


def _booked_event():
    return _event([_slot(1, "Alpha", 11), _slot(2, "Beta", 22)])


DJS = {
    11: _dj(11, "Alpha", "https://a.example/live", "https://a.example/alt"),
    22: _dj(22, "Beta", "https://b.example/live", None),
}


def test_lineup_state():
    assert lineup_state(_booked_event()) == "booked"
    assert lineup_state(_event([_slot(1, "Alpha", 11), _slot(2)])) == "open"
    assert lineup_state(_event([_slot(1, "Alpha", 11)], status="Canceled")) == "canceled"


def test_slot_order_and_stream_lines():
    text = _text(build_channel_messages(_booked_event(), DJS, "booked"))
    assert "Rave 1" in text and "05.12.2026" in text
    assert text.index("Slot 1, 20:00") < text.index("Slot 2, 21:00")
    assert "Alpha\nStream: https://a.example/live\nAlt stream: https://a.example/alt" in text
    assert "Beta\nStream: https://b.example/live\nAlt stream: none" in text
    assert "Techno" in text and "Club" in text and "Bring water" in text
    assert "Discord" not in text and "vrc" not in text


def test_open_state_note_and_open_slot():
    ev = _event([_slot(1, "Alpha", 11), _slot(2)])
    text = _text(build_channel_messages(ev, DJS, "open"))
    assert "No longer fully booked: open slots are marked." in text
    assert "Slot 2, 21:00\nopen" in text


def test_canceled_has_no_dj_lines():
    text = _text(build_channel_messages(_booked_event(), DJS, "canceled"))
    assert "This event was canceled." in text
    assert "Alpha" not in text and "Stream:" not in text


def test_many_slots_split_without_splitting_slots():
    link = "https://x.example/" + "a" * 182
    assert len(link) == 200
    slots = [_slot(n, f"DJ{n}", n) for n in range(1, 41)]
    djs = {n: _dj(n, f"DJ{n}", link, link) for n in range(1, 41)}
    ev = _event(slots)
    messages = build_channel_messages(ev, djs, "booked")
    assert len(messages) > 1
    for msg in messages:
        assert len(msg) <= 10
        assert sum(len(e) for e in msg) < 6000
    text = _text(messages)
    for n in range(1, 41):
        assert text.count(f"Slot {n}, ") == 1


def test_content_hash():
    a = build_channel_messages(_booked_event(), DJS, "booked")
    b = build_channel_messages(_booked_event(), DJS, "booked")
    assert content_hash(a) == content_hash(b)
    changed = {**DJS, 11: _dj(11, "Alpha", "https://new.example", "https://a.example/alt")}
    c = build_channel_messages(_booked_event(), changed, "booked")
    assert content_hash(c) != content_hash(a)


def test_dm_payloads():
    ev = _event([_slot(1, "Alpha", 11), _slot(2, "Guest", -1), _slot(3)], count=3)
    djs = {**DJS, -1: _dj(-1, "Guest", None, None, vrc="gv", logo="https://l.example/g.png")}
    payloads = build_dm_payloads(ev, djs, {11: "alpha#1"}, {11: b"PNG"})
    assert len(payloads) == 3
    assert all(isinstance(p, DmPayload) for p in payloads)
    head = payloads[0]
    assert "Rave 1" in head.text and "Techno" in head.text and "Club" in head.text
    assert head.logo is None
    first = payloads[1]
    assert first.logo == b"PNG"
    assert first.text.startswith("Slot 1, 20:00\nAlpha\nDiscord: alpha#1\nVRChat: vrc\n")
    assert "Stream: https://a.example/live\nAlt stream: https://a.example/alt" in first.text
    guest = payloads[2]
    assert "Discord: none" in guest.text
    assert guest.logo is None and "Logo: https://l.example/g.png" in guest.text
    assert "Alt stream: none" in guest.text
    assert first.logo_filename.endswith(".png")


def test_no_em_dash():
    ev = _event([_slot(1, "Alpha", 11), _slot(2)])
    out = _text(build_channel_messages(ev, DJS, "open"))
    out += "".join(p.text for p in build_dm_payloads(ev, DJS, {}, {}))
    assert "—" not in out
