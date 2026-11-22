"""Tests for the Booking rules page of the admin page (routes, form, escaping)."""
import aiohttp
import pytest

from bot.policy import BookingRules
from bot.results import ActionResult
from bot.ui import texts
from bot.web.auth import SessionManager
from tests.web.test_app import FakeService, _auth, make_client


class RulesService(FakeService):
    """The FakeService plus the booking rules methods backed by a fake store."""

    def __init__(self) -> None:
        super().__init__()
        self.rules = BookingRules()
        self.saved: list[dict[str, str]] = []

    async def booking_rules(self) -> BookingRules:
        return self.rules

    async def set_booking_rules(self, raw: dict[str, str]) -> ActionResult:
        from bot.policy import parse_rules_form

        parsed, errors = parse_rules_form(raw)
        if parsed is None:
            return ActionResult(False, " ".join(errors))
        self.saved.append(dict(raw))
        self.rules = parsed
        return ActionResult(True, "Booking rules saved.")


def _form(csrf: str, **fields: str) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("csrf", csrf)
    for key, value in fields.items():
        form.add_field(key, value)
    return form


async def test_get_shows_stored_values() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = RulesService()
    service.rules = BookingRules(True, 5, False, 90)
    async with make_client(service, sessions) as client:
        resp = await client.get("/booking-rules", headers=headers)
        text = await resp.text()
    assert resp.status == 200
    assert "Slot limit" in text and "Cancellation cutoff" in text
    assert 'name="slot_limit_enabled"' in text and 'name="cancel_cutoff_enabled"' in text
    assert 'name="slot_limit_max"' in text and 'value="5"' in text and 'value="90"' in text
    slot_box = text.split('name="slot_limit_enabled"')[1].split(">")[0]
    cutoff_box = text.split('name="cancel_cutoff_enabled"')[1].split(">")[0]
    assert "checked" in slot_box and "checked" not in cutoff_box
    assert "Save booking rules" in text and 'href="/"' in text
    assert "Existing bookings are kept." in text
    assert "0 means DJs can cancel until the event starts." in text


async def test_get_shows_defaults_when_nothing_stored() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(RulesService(), sessions) as client:
        text = await (await client.get("/booking-rules", headers=headers)).text()
    assert 'value="2"' in text and 'value="60"' in text
    assert text.count("checked") == 2


async def test_dashboard_has_link() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(RulesService(), sessions) as client:
        dash = await (await client.get("/", headers=headers)).text()
    assert 'href="/booking-rules"' in dash and "Booking rules" in dash


async def test_requires_login() -> None:
    async with make_client(RulesService()) as client:
        resp = await client.get("/booking-rules", allow_redirects=False)
        post = await client.post("/booking-rules", data={"a": "b"}, allow_redirects=False)
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert post.status == 303


async def test_post_without_csrf_refused() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = RulesService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/booking-rules", headers=headers,
            data=_form("wrong", slot_limit_max="3", cancel_cutoff_minutes="5"),
        )
    assert resp.status == 403 and service.saved == []


async def test_valid_post_saves_and_redirects_with_confirmation() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = RulesService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/booking-rules", headers=headers, allow_redirects=False,
            data=_form(
                csrf, slot_limit_enabled="on", slot_limit_max="3",
                cancel_cutoff_enabled="on", cancel_cutoff_minutes="120",
            ),
        )
        assert resp.status == 303
        assert resp.headers["Location"] == "/booking-rules?done=saved"
        text = await (await client.get("/booking-rules?done=saved", headers=headers)).text()
    assert service.rules == BookingRules(True, 3, True, 120)
    assert "Booking rules saved." in text


async def test_unchecked_boxes_store_false_and_numbers_are_saved() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = RulesService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/booking-rules", headers=headers, allow_redirects=False,
            data=_form(csrf, slot_limit_max="4", cancel_cutoff_minutes="0"),
        )
    assert resp.status == 303
    assert service.rules == BookingRules(False, 4, False, 0)


@pytest.mark.parametrize(
    ("limit", "cutoff"),
    [("", "60"), ("-1", "60"), ("abc", "60"), ("0", "60"), ("101", "60"),
     ("2", ""), ("2", "-1"), ("2", "abc"), ("2", "10081")],
)
async def test_invalid_numbers_give_400_keep_values_store_nothing(limit, cutoff) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = RulesService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/booking-rules", headers=headers,
            data=_form(
                csrf, slot_limit_enabled="on", slot_limit_max=limit,
                cancel_cutoff_minutes=cutoff,
            ),
        )
        text = await resp.text()
    assert resp.status == 400
    assert "must be a whole number" in text
    assert f'name="slot_limit_max" value="{limit}"' in text or f'value="{limit}"' in text
    slot_box = text.split('name="slot_limit_enabled"')[1].split(">")[0]
    cutoff_box = text.split('name="cancel_cutoff_enabled"')[1].split(">")[0]
    assert "checked" in slot_box and "checked" not in cutoff_box
    assert service.saved == [] and service.rules == BookingRules()


async def test_typed_value_is_escaped() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    async with make_client(RulesService(), sessions) as client:
        resp = await client.post(
            "/booking-rules", headers=headers,
            data=_form(
                csrf, slot_limit_max='"><script>alert(1)</script>', cancel_cutoff_minutes="1"
            ),
        )
        text = await resp.text()
    assert resp.status == 400
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


async def test_maintenance_refuses() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = RulesService()
    service.maintenance = lambda: texts.MAINTENANCE
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/booking-rules", headers=headers,
            data=_form(csrf, slot_limit_max="3", cancel_cutoff_minutes="5"),
        )
        text = await resp.text()
    assert resp.status == 503 and texts.MAINTENANCE in text
    assert service.saved == []
