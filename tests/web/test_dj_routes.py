"""Tests for the DJ pages of the admin page (routes, forms, uploads, limits)."""

import aiohttp
import pytest

from bot.services.dj_admin import DjResult
from bot.web.auth import SessionManager
from tests.notion.factories import make_event
from tests.web.test_app import FakeService, _auth, make_client

ROW = {
    "discord_user_id": -1, "dj_name": "Moonbeat <b>", "vrchat_name": "Moon",
    "stream_url_primary": "https://twitch.tv/moon", "stream_url_backup": None,
    "active_booking": False, "has_logo": True,
}
DISCORD_ROW = {**ROW, "discord_user_id": 123456789012345678, "dj_name": "Nova"}


class FakeDjs:
    def __init__(self) -> None:
        self.rows = [ROW, DISCORD_ROW]
        self.calls: list[tuple] = []
        self.result = DjResult(True, "Done.")
        self.bookings = []

    async def list_djs(self):
        return self.rows

    async def get_dj(self, dj_id):
        return next((r for r in self.rows if r["discord_user_id"] == dj_id), None)

    async def add_dj(self, **kwargs):
        self.calls.append(("add", kwargs))
        return self.result

    async def edit_dj(self, **kwargs):
        self.calls.append(("edit", kwargs))
        return self.result

    async def delete_dj(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return self.result

    async def future_bookings(self, dj_id):
        return self.bookings


def _service() -> FakeService:
    service = FakeService()
    service.djs = FakeDjs()
    return service


def _form(csrf: str, **fields) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("csrf", csrf)
    for key, value in fields.items():
        if isinstance(value, tuple):
            form.add_field(key, value[1], filename=value[0], content_type="image/png")
        else:
            form.add_field(key, value)
    return form


async def test_dashboard_links_to_the_dj_page() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(_service(), sessions) as client:
        text = await (await client.get("/", headers=headers)).text()
    assert 'href="/djs"' in text


async def test_dj_list_escapes_and_marks_guests() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(_service(), sessions) as client:
        text = await (await client.get("/djs", headers=headers)).text()
    assert "Moonbeat &lt;b&gt;" in text and "<b>" not in text.split("Moonbeat")[1][:12]
    assert ">Guest<" in text and ">Discord<" in text
    assert 'type="file"' in text and 'enctype="multipart/form-data"' in text


async def test_add_dj_with_logo_and_guest() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/djs/add",
            headers=headers,
            data=_form(csrf, name="Moon", link="twitch.tv/m", alt_link="", vrchat="",
                       discord_id="", logo=("l.png", b"PNGDATA")),
        )
        assert resp.status == 200 and "Done." in await resp.text()
    kind, kwargs = service.djs.calls[0]
    assert kind == "add" and kwargs["name"] == "Moon" and kwargs["discord_id"] is None
    assert kwargs["logo"] == b"PNGDATA"


async def test_add_dj_without_a_file_has_no_logo_and_bad_id_is_rejected() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        await client.post(
            "/djs/add", headers=headers,
            data=_form(csrf, name="A", link="https://a.b", discord_id="123456789012345678"),
        )
        resp = await client.post(
            "/djs/add", headers=headers,
            data=_form(csrf, name="B", link="https://a.b", discord_id="nope"),
        )
        assert resp.status == 400 and 'value="B"' in await resp.text()
    assert len(service.djs.calls) == 1
    assert service.djs.calls[0][1]["logo"] is None
    assert service.djs.calls[0][1]["discord_id"] == 123456789012345678


async def test_add_dj_failure_keeps_the_typed_values() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    service.djs.result = DjResult(False, "There is already a DJ called \"X\".")
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/djs/add", headers=headers, data=_form(csrf, name="X", link="https://a.b")
        )
        text = await resp.text()
    assert resp.status == 400 and "already a DJ" in text and 'value="https://a.b"' in text


async def test_dj_posts_need_csrf_and_login() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        bad = await client.post("/djs/add", headers=headers, data=_form("wrong", name="A"))
        assert bad.status == 403
        anon = await client.post(
            "/djs/delete", data={"id": "-1", "csrf": "x"}, allow_redirects=False
        )
        assert anon.status == 303 and anon.headers["Location"] == "/login"
    assert service.djs.calls == []


async def test_edit_form_and_save() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        page = await client.get("/djs/edit?id=-1", headers=headers)
        text = await page.text()
        assert page.status == 200 and 'value="https://twitch.tv/moon"' in text
        assert "A logo is saved" in text
        resp = await client.post(
            "/djs/edit", headers=headers,
            data=_form(csrf, id="-1", name="Moon", link="https://a.b", alt_link="", vrchat=""),
        )
        assert resp.status == 200
        missing = await client.get("/djs/edit?id=77", headers=headers)
        assert missing.status == 404
        junk = await client.get("/djs/edit?id=abc", headers=headers)
        assert junk.status == 404
    kind, kwargs = service.djs.calls[0]
    assert kind == "edit" and kwargs["dj_id"] == -1
    assert kwargs["fields"] == {"name": "Moon", "link": "https://a.b", "alt_link": "", "vrchat": ""}


async def test_edit_failure_shows_the_form_again() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    service.djs.result = DjResult(False, "The DJ name is required.")
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/djs/edit", headers=headers,
            data=_form(csrf, id="-1", name="", link="https://a.b", alt_link="", vrchat=""),
        )
        text = await resp.text()
    assert resp.status == 400 and "name is required" in text and "Edit a DJ" in text


async def test_delete_confirm_lists_bookings_then_deletes() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    service.djs.bookings = [(make_event(event_id="Rave One"), 0)]
    async with make_client(service, sessions) as client:
        text = await (await client.get("/djs/delete?id=-1", headers=headers)).text()
        assert "Rave One" in text and "cancelled" in text
        resp = await client.post(
            "/djs/delete", headers=headers, data=_form(csrf, id="-1")
        )
        assert resp.status == 200
    assert service.djs.calls == [("delete", {"admin_name": "The admin page", "dj_id": -1})]


async def test_big_bodies_are_only_allowed_on_the_upload_routes() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    async with make_client(_service(), sessions) as client:
        big = await client.post(
            "/admins/add", headers=headers, data={"csrf": csrf, "id": "1" * 6000}
        )
        assert big.status == 413
        logo = await client.post(
            "/djs/add", headers=headers,
            data=_form(csrf, name="A", link="https://a.b", logo=("l.png", b"x" * 100_000)),
        )
        assert logo.status == 200
        huge = await client.post(
            "/djs/add", headers=headers,
            data=_form(csrf, name="A", link="https://a.b", logo=("l.png", b"x" * 7_000_000)),
        )
        assert huge.status == 413


async def test_logout_ends_the_session_for_a_copied_cookie_too() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    async with make_client(_service(), sessions) as client:
        await client.post("/logout", headers=headers, data={"csrf": csrf}, allow_redirects=False)
        resp = await client.get("/djs", headers=headers, allow_redirects=False)
    assert resp.status == 303 and resp.headers["Location"] == "/login"


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/djs"), ("GET", "/djs/edit?id=-1"), ("GET", "/djs/delete?id=-1"),
     ("POST", "/djs/add"), ("POST", "/djs/edit"), ("POST", "/djs/delete")],
)
async def test_every_dj_route_needs_login(method, path) -> None:
    service = _service()
    async with make_client(service) as client:
        resp = await client.request(method, path, data={"id": "-1", "csrf": "x"},
                                    allow_redirects=False)
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert service.djs.calls == []


@pytest.mark.parametrize("path", ["/djs/add", "/djs/edit", "/djs/delete"])
async def test_every_dj_post_needs_csrf(path) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        resp = await client.post(path, headers=headers, data={"id": "-1", "csrf": "wrong"})
    assert resp.status == 403
    assert service.djs.calls == []
