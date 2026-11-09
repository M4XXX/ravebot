"""Tests for bot/web/app.py (routes, sessions, CSRF, throttle, headers)."""
import asyncio
import html
import logging
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

import bot.web.app as web_app
from bot.web.app import create_app
from bot.web.auth import COOKIE_NAME, SESSION_LIFETIME_SECONDS, LoginThrottle, SessionManager
from bot.web.service import ActionResult, AdminRow, ServerInfo, UserInfo

PASSWORD = "S3cret-Pass-xyz-9071"
OWNER = 111111111111111111
ADDED = 222222222222222222
XSS_SCRIPT = "<script>alert(1)</script>"
XSS_IMG = '"><img src=x onerror=alert(1)>'

HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; img-src https://cdn.discordapp.com; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeService:
    def __init__(self, added_name: str = "Luna") -> None:
        self.calls: list[tuple] = []
        self.owners = {OWNER: "Boss"}
        self.added = {ADDED: added_name}
        self.add_result = ActionResult(True, "Added Nova.")
        self.fail_list = False

    def server_info(self):
        self.calls.append(("server_info",))
        return ServerInfo(999999999999999999, "Rave Galaxy", None)

    def _row(self, uid: int) -> AdminRow:
        owner = uid in self.owners
        name = self.owners[uid] if owner else self.added[uid]
        return AdminRow(UserInfo(uid, name, None, True), owner)

    async def list_admins(self):
        self.calls.append(("list_admins",))
        if self.fail_list:
            raise RuntimeError("boom")
        return [self._row(uid) for uid in [*self.owners, *self.added]]

    async def admin_row(self, raw_id: str):
        self.calls.append(("admin_row", raw_id))
        try:
            uid = int(raw_id.strip())
        except ValueError:
            return None
        if uid in self.owners or uid in self.added:
            return self._row(uid)
        return None

    async def add_admin(self, raw_id: str):
        self.calls.append(("add_admin", raw_id))
        return self.add_result

    async def remove_admin(self, raw_id: str):
        self.calls.append(("remove_admin", raw_id))
        try:
            uid = int(raw_id.strip())
        except ValueError:
            return ActionResult(False, "Please enter a Discord user ID (17 to 20 digits).")
        if uid in self.owners:
            return ActionResult(False, "Owners can only be changed with ADMIN_USER_IDS.")
        if uid not in self.added:
            return ActionResult(False, "This person is not an added admin.")
        name = self.added.pop(uid)
        return ActionResult(True, f"Removed {name}.")

    def mutating_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("add_admin", "remove_admin")]


@asynccontextmanager
async def make_client(service=None, sessions=None, throttle=None):
    app = create_app(
        service or FakeService(),
        PASSWORD,
        sessions=sessions or SessionManager(secret=b"k" * 32),
        throttle=throttle,
        fail_delay=0,
    )
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _auth(sessions: SessionManager) -> tuple[dict[str, str], str]:
    token, session = sessions.create()
    return {"Cookie": f"{COOKIE_NAME}={token}"}, session.csrf


def _assert_headers(resp: aiohttp.ClientResponse) -> None:
    for name, value in HEADERS.items():
        assert resp.headers.get(name) == value, (name, resp.status)


ROUTES = [
    ("GET", "/"),
    ("GET", "/admins/remove?id=1"),
    ("GET", "/admins/remove"),
    ("GET", "/does-not-exist"),
    ("POST", "/"),
    ("POST", "/admins/add"),
    ("POST", "/admins/remove"),
    ("POST", "/logout"),
    ("PUT", "/admins/add"),
    ("DELETE", "/"),
]


@pytest.mark.parametrize("method,path", ROUTES)
async def test_unauthenticated_matrix_redirects_to_login(method, path) -> None:
    service = FakeService()
    async with make_client(service) as client:
        resp = await client.request(
            method, path, data={"id": str(ADDED), "csrf": "x"}, allow_redirects=False
        )
        assert resp.status == 303
        assert resp.headers["Location"] == "/login"
        _assert_headers(resp)
    assert service.calls == []


@pytest.mark.parametrize("method,path", [("GET", "/"), ("POST", "/admins/remove")])
async def test_tampered_cookie_rejected(method, path) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    token, session = sessions.create()
    forged = SessionManager(secret=b"z" * 32).create()[0]
    service = FakeService()
    async with make_client(service, sessions) as client:
        for bad in (token[:-2] + ("AA" if token[-2:] != "AA" else "BB"), forged, "x.y", ""):
            resp = await client.request(
                method,
                path,
                headers={"Cookie": f"{COOKIE_NAME}={bad}"},
                data={"id": str(ADDED), "csrf": session.csrf},
                allow_redirects=False,
            )
            assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert service.mutating_calls() == []
    assert ADDED in service.added


async def test_expired_cookie_rejected() -> None:
    clock = Clock()
    sessions = SessionManager(secret=b"k" * 32, clock=clock)
    headers, _ = _auth(sessions)
    async with make_client(sessions=sessions) as client:
        assert (await client.get("/", headers=headers, allow_redirects=False)).status == 200
        clock.now += SESSION_LIFETIME_SECONDS + 1
        resp = await client.get("/", headers=headers, allow_redirects=False)
        assert resp.status == 303 and resp.headers["Location"] == "/login"


async def test_login_page() -> None:
    async with make_client() as client:
        resp = await client.get("/login")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert 'name="password"' in await resp.text()
        _assert_headers(resp)


async def test_login_page_with_session_goes_to_dashboard() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(sessions=sessions) as client:
        resp = await client.get("/login", headers=headers, allow_redirects=False)
        assert resp.status == 303 and resp.headers["Location"] == "/"


async def test_login_wrong_password() -> None:
    async with make_client() as client:
        resp = await client.post("/login", data={"password": "nope"}, allow_redirects=False)
        assert resp.status == 401
        assert "Wrong password." in await resp.text()
        assert "Set-Cookie" not in resp.headers
        _assert_headers(resp)


async def test_login_correct_password_sets_cookie() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    async with make_client(sessions=sessions) as client:
        resp = await client.post("/login", data={"password": PASSWORD}, allow_redirects=False)
        assert resp.status == 303 and resp.headers["Location"] == "/"
        cookie = resp.headers["Set-Cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        assert f"Max-Age={SESSION_LIFETIME_SECONDS}" in cookie
        _assert_headers(resp)
        token = resp.cookies[COOKIE_NAME].value
        assert sessions.verify(token) is not None
        ok = await client.get("/", headers={"Cookie": f"{COOKIE_NAME}={token}"})
        assert ok.status == 200


async def test_throttle_blocks_then_correct_password_refused() -> None:
    async with make_client(throttle=LoginThrottle(clock=Clock())) as client:
        for _ in range(5):
            resp = await client.post("/login", data={"password": "bad"})
            assert resp.status == 401
        resp = await client.post("/login", data={"password": PASSWORD}, allow_redirects=False)
        assert resp.status == 429
        assert "Too many attempts. Try again in 5 minutes." in await resp.text()
        assert "Set-Cookie" not in resp.headers
        _assert_headers(resp)


async def test_throttle_ignores_x_forwarded_for() -> None:
    async with make_client(throttle=LoginThrottle(clock=Clock())) as client:
        for i in range(5):
            resp = await client.post(
                "/login", data={"password": "bad"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}
            )
            assert resp.status == 401
        resp = await client.post(
            "/login",
            data={"password": PASSWORD},
            headers={"X-Forwarded-For": "10.9.9.9"},
            allow_redirects=False,
        )
        assert resp.status == 429


async def test_success_resets_throttle() -> None:
    async with make_client(throttle=LoginThrottle(clock=Clock())) as client:
        for _ in range(4):
            await client.post("/login", data={"password": "bad"})
        ok = await client.post("/login", data={"password": PASSWORD}, allow_redirects=False)
        assert ok.status == 303
        for _ in range(4):
            assert (await client.post("/login", data={"password": "bad"})).status == 401


@pytest.mark.parametrize(
    "data",
    [None, {"password": "x", "other": "y"}],
)
async def test_login_missing_field_is_400(data) -> None:
    async with make_client() as client:
        body = None if data is None else {k: v for k, v in data.items() if k != "password"}
        resp = await client.post("/login", data=body)
        assert resp.status == 400
        _assert_headers(resp)


async def test_login_wrong_content_types() -> None:
    async with make_client() as client:
        resp = await client.post("/login", json={"password": PASSWORD}, allow_redirects=False)
        assert resp.status == 400
        assert "Set-Cookie" not in resp.headers
        resp = await client.post(
            "/login",
            data=b"--zz\r\nbroken",
            headers={"Content-Type": "multipart/form-data; boundary=zz"},
        )
        assert resp.status == 400
        _assert_headers(resp)


@pytest.mark.parametrize("path", ["/admins/add", "/admins/remove", "/logout"])
@pytest.mark.parametrize("csrf", [None, "wrong", ""])
async def test_csrf_required_on_posts(path, csrf) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = FakeService()
    data = {"id": str(ADDED)}
    if csrf is not None:
        data["csrf"] = csrf
    async with make_client(service, sessions) as client:
        resp = await client.post(path, data=data, headers=headers, allow_redirects=False)
        assert resp.status == 403
        assert "Set-Cookie" not in resp.headers
        _assert_headers(resp)
    assert service.calls == []
    assert ADDED in service.added


async def test_csrf_from_other_session_rejected() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    _, other_csrf = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/admins/remove", data={"id": str(ADDED), "csrf": other_csrf}, headers=headers
        )
        assert resp.status == 403
    assert service.mutating_calls() == []


async def test_add_calls_service_once_with_raw_field() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/admins/add", data={"id": " 333 ", "csrf": csrf}, headers=headers,
            allow_redirects=False,
        )
        assert resp.status == 200
        body = await resp.text()
        assert "Added Nova." in body and "Admins with bot commands (2)" in body
        assert service.mutating_calls() == [("add_admin", " 333 ")]
        service.add_result = ActionResult(False, XSS_IMG)
        resp = await client.post("/admins/add", data={"id": "1", "csrf": csrf}, headers=headers)
        assert resp.status == 400
        body = await resp.text()
        assert XSS_IMG not in body and html.escape(XSS_IMG, quote=True) in body


async def test_add_missing_field_is_400_without_service_call() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.post("/admins/add", data={"csrf": csrf}, headers=headers)
        assert resp.status == 400
        resp = await client.post("/admins/remove", data={"csrf": csrf}, headers=headers)
        assert resp.status == 400
    assert service.mutating_calls() == []


async def test_remove_calls_service_once() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/admins/remove", data={"id": str(ADDED), "csrf": csrf}, headers=headers
        )
        assert resp.status == 200
        body = await resp.text()
        assert "Removed Luna." in body
        assert "Admins with bot commands (1)" in body
    assert service.mutating_calls() == [("remove_admin", str(ADDED))]


async def test_crafted_remove_of_owner_refused() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/admins/remove", data={"id": str(OWNER), "csrf": csrf}, headers=headers
        )
        assert resp.status == 400
        body = await resp.text()
        assert "Owners can only be changed with ADMIN_USER_IDS." in body
        assert str(OWNER) in body and "Boss" in body
        assert "Admins with bot commands (2)" in body


async def test_confirm_page_for_added_admin() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.get(f"/admins/remove?id={ADDED}", headers=headers)
        assert resp.status == 200
        body = await resp.text()
        assert "Remove Luna?" in body
        assert f'name="csrf" value="{csrf}"' in body
        assert f'name="id" value="{ADDED}"' in body
        _assert_headers(resp)
    assert service.mutating_calls() == []


@pytest.mark.parametrize(
    "query,message",
    [
        (f"?id={OWNER}", "Owners can only be changed with ADMIN_USER_IDS."),
        ("?id=333333333333333333", "This person is not an added admin."),
        ("?id=abc", "This person is not an added admin."),
        ("", "This person is not an added admin."),
    ],
)
async def test_confirm_page_refuses_owner_and_unknown(query, message) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.get(f"/admins/remove{query}", headers=headers)
        assert resp.status == 400
        body = await resp.text()
        assert message in body
        assert 'action="/admins/remove"' not in body
        assert "Admins with bot commands" in body
    assert service.mutating_calls() == []


async def test_confirm_and_dashboard_escape_names() -> None:
    for name in (XSS_SCRIPT, XSS_IMG):
        sessions = SessionManager(secret=b"k" * 32)
        headers, _ = _auth(sessions)
        async with make_client(FakeService(added_name=name), sessions) as client:
            for path in (f"/admins/remove?id={ADDED}", "/"):
                body = await (await client.get(path, headers=headers)).text()
                assert name not in body
                assert html.escape(name, quote=True) in body


async def test_logout_clears_cookie() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    async with make_client(sessions=sessions) as client:
        resp = await client.post(
            "/logout", data={"csrf": csrf}, headers=headers, allow_redirects=False
        )
        assert resp.status == 303 and resp.headers["Location"] == "/login"
        cookie = resp.headers["Set-Cookie"]
        assert cookie.startswith(f'{COOKIE_NAME}=""') or cookie.startswith(f"{COOKIE_NAME}=;")
        assert "Max-Age=0" in cookie and "Path=/" in cookie
        _assert_headers(resp)


async def test_unknown_path_404_with_headers() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(sessions=sessions) as client:
        resp = await client.get("/nope", headers=headers)
        assert resp.status == 404
        assert "<script" not in await resp.text()
        _assert_headers(resp)
        resp = await client.get("/logout", headers=headers)
        assert resp.status == 405
        _assert_headers(resp)


async def test_server_error_has_headers_and_plain_page() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = FakeService()
    service.fail_list = True
    async with make_client(service, sessions) as client:
        resp = await client.get("/", headers=headers)
        assert resp.status == 500
        assert "boom" not in await resp.text()
        _assert_headers(resp)


async def test_oversized_bodies_rejected() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = FakeService()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/admins/add", data={"id": "1" * 5000, "csrf": csrf}, headers=headers
        )
        assert resp.status == 413
        _assert_headers(resp)
        resp = await client.post("/login", data={"password": "x" * 5000})
        assert resp.status == 413
        _assert_headers(resp)
    assert service.calls == []


async def test_password_never_in_bodies_or_logs(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []
    throttle = LoginThrottle(clock=Clock())
    async with make_client(throttle=throttle) as client:
        for data in ({"password": PASSWORD + "x"}, {"password": PASSWORD}):
            resp = await client.post("/login", data=data, allow_redirects=False)
            bodies.append(await resp.text())
            bodies.append(str(resp.headers))
        for _ in range(5):
            resp = await client.post("/login", data={"password": PASSWORD[:-1]})
            bodies.append(await resp.text())
        resp = await client.post("/login", data={"password": PASSWORD})
        assert resp.status == 429
        bodies.append(await resp.text())
        resp = await client.post("/login", json={"password": PASSWORD})
        bodies.append(await resp.text())
        bodies.append((await client.get("/login")).headers.get("Set-Cookie", ""))
    for text in bodies:
        assert PASSWORD[:-1] not in text
    assert PASSWORD[:-1] not in caplog.text
    for record in caplog.records:
        assert PASSWORD[:-1] not in record.getMessage()
        assert PASSWORD[:-1] not in str(record.args)


async def _raw_login_headers(host: str, port: int, body: bytes):
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        b"POST /login HTTP/1.1\r\nHost: x\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
    )
    await writer.drain()
    return reader, writer


async def _send_body(conn, body: bytes) -> bytes:
    reader, writer = conn
    writer.write(body)
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


async def test_parallel_logins_cannot_bypass_throttle(monkeypatch) -> None:
    evaluated: list[bool] = []
    real_check = web_app.check_password

    def counting_check(given: str, expected: str) -> bool:
        result = real_check(given, expected)
        evaluated.append(result)
        return result

    monkeypatch.setattr(web_app, "check_password", counting_check)
    wrong = b"password=wrong"
    right = f"password={PASSWORD}".encode()
    async with make_client(throttle=LoginThrottle(clock=Clock())) as client:
        host, port = str(client.host), int(client.port or 0)
        wrong_conns = [await _raw_login_headers(host, port, wrong) for _ in range(30)]
        right_conn = await _raw_login_headers(host, port, right)
        await asyncio.sleep(0.2)  # every handler is now waiting for its body
        answers = await asyncio.gather(*(_send_body(c, wrong) for c in wrong_conns))
        final = await _send_body(right_conn, right)
    statuses = [a.split(b" ", 2)[1] for a in answers]
    assert statuses.count(b"401") == 5
    assert statuses.count(b"429") == 25
    assert final.split(b" ", 2)[1] == b"429"
    assert b"Set-Cookie" not in final and COOKIE_NAME.encode() not in final
    assert evaluated.count(False) <= 5
    assert True not in evaluated


async def test_blocked_login_also_waits(monkeypatch) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(web_app.asyncio, "sleep", fake_sleep)
    app = create_app(
        FakeService(), PASSWORD, throttle=LoginThrottle(clock=Clock()), fail_delay=0.25
    )
    async with TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar()) as client:
        for _ in range(5):
            assert (await client.post("/login", data={"password": "bad"})).status == 401
        assert delays == [0.25] * 5
        resp = await client.post("/login", data={"password": PASSWORD})
        assert resp.status == 429
        assert delays == [0.25] * 6
