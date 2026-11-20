import datetime

from bot.sync.payloads import dj_payload, event_payload, payload_hash, slot_payload


def test_hash_ignores_key_order():
    assert payload_hash({"a": 1, "b": [1, 2]}) == payload_hash({"b": [1, 2], "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_event_payload_shape():
    row = {"event_id": "Fri", "date": datetime.date(2026, 6, 13), "start": "20:00",
           "slot_minutes": 60, "slot_count": 4, "status": "Planned", "genre": "",
           "notes": None}
    assert event_payload(row) == {
        "event_id": "Fri", "date": "2026-06-13", "start": "20:00", "slot_minutes": 60,
        "slot_count": 4, "status": "Planned", "genre": None, "notes": None}


def test_slot_payload_number_is_index_plus_one():
    assert slot_payload("u", 0, None) == {"event_uid": "u", "number": 1, "discord_id": None}


def test_dj_payload_maps_stream_urls():
    row = {"discord_user_id": 5, "dj_name": "A", "vrchat_name": None, "logo_url": None,
           "stream_url_primary": "https://a.test", "stream_url_backup": None,
           "logo_image": b"x"}
    assert dj_payload(row) == {
        "discord_id": 5, "dj_name": "A", "vrchat_name": None,
        "link": "https://a.test", "alt_link": None}


def test_dj_payload_drops_non_http_links_like_the_notion_writer():
    row = {"discord_user_id": 5, "dj_name": "A", "vrchat_name": None,
           "stream_url_primary": "ftp://a.test", "stream_url_backup": " https://b.test "}
    p = dj_payload(row)
    assert p["link"] is None
    assert p["alt_link"] == "https://b.test"
