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


# --- data and backup, Notion sync (Task 17) ---

import datetime  # noqa: E402

from bot.db.issues import IssueRow  # noqa: E402
from bot.db.outbox import OutboxStats  # noqa: E402
from bot.web.service import SyncStatus  # noqa: E402

NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.UTC)


def _issue(issue_id=7, kind="conflict", summary="Moon changed"):
    return IssueRow(
        id=issue_id, kind=kind, entity="dj", entity_key="42", summary=summary,
        pg_payload={"dj_name": "Moon"}, notion_payload={"dj_name": "Moon2"},
        created_at=NOW, resolved_at=None, resolution=None,
    )


class SyncFakeService(FakeService):
    def __init__(self, enabled: bool = True) -> None:
        super().__init__()
        self.enabled = enabled
        self.issues = [_issue(), _issue(8, "warning", "Event deleted in Notion")]
        self.problem: str | None = None
        self.can_retry = False
        self.uploads: list[bytes] = []

    async def sync_status(self):
        return SyncStatus(
            enabled=self.enabled, last_run=NOW, last_error=None,
            outbox=OutboxStats(3, NOW, None), issues=self.issues if self.enabled else [],
            problem=self.problem, maintenance=None, can_retry=self.can_retry,
        )

    async def export_backup(self) -> bytes:
        self.calls.append(("export_backup",))
        return b"\x1f\x8bbackup"

    async def import_backup(self, data: bytes):
        self.calls.append(("import_backup", len(data)))
        self.uploads.append(data)
        return ActionResult(True, "The backup was restored.")

    async def rebuild_from_notion(self):
        self.calls.append(("rebuild_from_notion",))
        return ActionResult(True, "Rebuilt.")

    async def resync_all(self):
        self.calls.append(("resync_all",))
        return ActionResult(True, "Queued.")

    async def open_issue(self, raw_id: str):
        return next((i for i in self.issues if str(i.id) == raw_id), None)

    async def resolve_issue(self, raw_id: str, choice: str):
        self.calls.append(("resolve_issue", raw_id, choice))
        return ActionResult(True, "Notion's version is applied.")

    async def retry_import(self):
        self.calls.append(("retry_import",))
        return ActionResult(True, "Imported.")


async def test_export_requires_login() -> None:
    service = SyncFakeService()
    async with make_client(service) as client:
        resp = await client.get("/backup/export", allow_redirects=False)
        assert resp.status == 303
        assert resp.headers["Location"] == "/login"
    assert ("export_backup",) not in service.calls


async def test_export_downloads_file() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = SyncFakeService()
    async with make_client(service, sessions) as client:
        resp = await client.get("/backup/export", headers=headers)
        assert resp.status == 200
        assert await resp.read() == b"\x1f\x8bbackup"
        disposition = resp.headers["Content-Disposition"]
        assert disposition.startswith('attachment; filename="ravebot-')
        assert disposition.endswith('.ravebot.json.gz"')
        _assert_headers(resp)


async def test_import_needs_csrf() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService()
    async with make_client(service, sessions) as client:
        page = await (await client.get("/backup/import", headers=headers)).text()
        assert 'action="/backup/import"' in page and 'enctype="multipart/form-data"' in page
        form = aiohttp.FormData()
        form.add_field("csrf", "wrong")
        form.add_field("backup", b"data", filename="b.ravebot.json.gz")
        resp = await client.post("/backup/import", headers=headers, data=form)
        assert resp.status == 403
        assert service.uploads == []
        form = aiohttp.FormData()
        form.add_field("csrf", csrf)
        form.add_field("backup", b"data", filename="b.ravebot.json.gz")
        resp = await client.post("/backup/import", headers=headers, data=form)
        assert resp.status == 200
        assert "The backup was restored." in await resp.text()
    assert service.uploads == [b"data"]


async def test_import_without_file_is_refused() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService()
    async with make_client(service, sessions) as client:
        form = aiohttp.FormData()
        form.add_field("csrf", csrf)
        form.add_field("backup", b"", filename="")
        resp = await client.post("/backup/import", headers=headers, data=form)
        assert resp.status == 400
    assert service.uploads == []


async def test_import_large_file_accepted_other_paths_limited() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService()
    big = b"x" * (web_app.MAX_UPLOAD + 1024 * 1024)
    assert web_app.MAX_BACKUP == 512 * 1024 * 1024
    async with make_client(service, sessions) as client:
        form = aiohttp.FormData()
        form.add_field("csrf", csrf)
        form.add_field("backup", big, filename="b.ravebot.json.gz")
        resp = await client.post("/backup/import", headers=headers, data=form)
        assert resp.status == 200
        for path in ("/djs/add", "/djs/edit"):
            form = aiohttp.FormData()
            form.add_field("csrf", csrf)
            form.add_field("logo", big, filename="logo.png", content_type="image/png")
            resp = await client.post(path, headers=headers, data=form)
            assert resp.status == 413, path
            _assert_headers(resp)
        resp = await client.post(
            "/admins/add", headers=headers,
            data={"id": "1" * 5000, "csrf": csrf},
        )
        assert resp.status == 413
    assert service.uploads == [big]


async def test_chunked_upload_on_logo_path_limited() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService()

    async def chunks():
        boundary = b"--B\r\n"
        yield boundary + b'Content-Disposition: form-data; name="csrf"\r\n\r\n' + csrf.encode()
        yield b"\r\n" + boundary
        yield b'Content-Disposition: form-data; name="logo"; filename="a.png"\r\n\r\n'
        for _ in range(8):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--B--\r\n"

    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/djs/add", headers={**headers, "Content-Type": "multipart/form-data; boundary=B"},
            data=chunks(),
        )
        assert resp.status == 413


async def test_notion_card_hidden_when_disabled() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(SyncFakeService(enabled=False), sessions) as client:
        text = await (await client.get("/", headers=headers)).text()
        assert "Data and backup" in text and 'href="/backup/export"' in text
        assert "Notion sync" not in text and "/sync/rebuild" not in text
    async with make_client(SyncFakeService(enabled=True), sessions) as client:
        text = await (await client.get("/", headers=headers)).text()
        assert "Data and backup" in text and "Notion sync" in text
        assert 'href="/sync/rebuild"' in text and 'href="/sync/resync"' in text
        assert "Keep Postgres" in text and "Keep Notion" in text
        assert "Resync Postgres to Notion" in text


async def test_issue_keep_notion_calls_resolve() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService()
    async with make_client(service, sessions) as client:
        confirm = await client.get("/sync/issues/7?choice=notion", headers=headers)
        assert confirm.status == 200
        page = await confirm.text()
        assert 'action="/sync/issues/7"' in page and 'value="notion"' in page
        assert "Moon2" in page
        bad = await client.post("/sync/issues/7", headers=headers,
                                data={"csrf": "x", "choice": "notion"})
        assert bad.status == 403
        assert not [c for c in service.calls if c[0] == "resolve_issue"]
        resp = await client.post("/sync/issues/7", headers=headers,
                                 data={"csrf": csrf, "choice": "notion"})
        assert resp.status == 200
        assert "Notion&#x27;s version is applied." in await resp.text()
    assert ("resolve_issue", "7", "notion") in service.calls


async def test_issue_confirm_unknown_or_wrong_choice() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(SyncFakeService(), sessions) as client:
        resp = await client.get("/sync/issues/99?choice=notion", headers=headers)
        assert resp.status == 404
        resp = await client.get("/sync/issues/7?choice=resynced", headers=headers)
        assert resp.status == 400
        resp = await client.get("/sync/issues/7?choice=evil", headers=headers)
        assert resp.status == 400


async def test_rebuild_confirm_page() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService()
    async with make_client(service, sessions) as client:
        page = await (await client.get("/sync/rebuild", headers=headers)).text()
        assert 'action="/sync/rebuild"' in page and "safety backup" in page
        assert ("rebuild_from_notion",) not in service.calls
        resp = await client.post("/sync/rebuild", headers=headers, data={"csrf": csrf})
        assert resp.status == 200 and "Rebuilt." in await resp.text()
    assert service.calls.count(("rebuild_from_notion",)) == 1


@pytest.mark.parametrize(
    ("path", "call"),
    [("/sync/resync", "resync_all"), ("/sync/retry-import", "retry_import")],
)
async def test_sync_actions_have_confirm_and_need_csrf(path, call) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService()
    service.problem = "Duplicate title"
    async with make_client(service, sessions) as client:
        page = await (await client.get(path, headers=headers)).text()
        assert f'action="{path}"' in page
        bad = await client.post(path, headers=headers, data={"csrf": "x"})
        assert bad.status == 403
        assert (call,) not in service.calls
        ok = await client.post(path, headers=headers, data={"csrf": csrf})
        assert ok.status == 200
    assert (call,) in service.calls


async def test_sync_pages_hidden_when_notion_disabled() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    async with make_client(SyncFakeService(enabled=False), sessions) as client:
        for path in ("/sync/rebuild", "/sync/resync", "/sync/retry-import",
                     "/sync/issues/7?choice=notion"):
            resp = await client.get(path, headers=headers, allow_redirects=False)
            assert resp.status == 303, path


async def test_retry_button_only_with_sync_problem() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = SyncFakeService()
    async with make_client(service, sessions) as client:
        text = await (await client.get("/", headers=headers)).text()
        assert "/sync/retry-import" not in text
        service.problem = "Two events named <b>X</b>"
        text = await (await client.get("/", headers=headers)).text()
        assert 'href="/sync/retry-import"' in text
        assert "<b>X</b>" not in text and "&lt;b&gt;X&lt;/b&gt;" in text


async def test_retry_reachable_while_notion_not_connected() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = SyncFakeService(enabled=False)
    service.problem = "The Notion databases are missing"
    async with make_client(service, sessions) as client:
        resp = await client.get("/sync/retry-import", headers=headers, allow_redirects=False)
        assert resp.status == 303
        service.can_retry = True
        text = await (await client.get("/", headers=headers)).text()
        assert 'href="/sync/retry-import"' in text
        resp = await client.get("/sync/retry-import", headers=headers, allow_redirects=False)
        assert resp.status == 200
        assert "The Notion databases are missing" in await resp.text()
        for path in ("/sync/rebuild", "/sync/resync"):
            resp = await client.get(path, headers=headers, allow_redirects=False)
            assert resp.status == 303, path
        ok = await client.post("/sync/retry-import", headers=headers, data={"csrf": csrf})
        assert ok.status == 200
    assert ("retry_import",) in service.calls


@pytest.mark.parametrize(
    "path", ["/backup/export", "/backup/import", "/sync/rebuild", "/sync/resync",
             "/sync/retry-import", "/sync/issues/7"],
)
async def test_new_routes_redirect_without_login(path) -> None:
    service = SyncFakeService()
    async with make_client(service) as client:
        for method in ("GET", "POST"):
            resp = await client.request(method, path, allow_redirects=False)
            assert resp.status in (303, 405), (method, path)
            if resp.status == 303:
                assert resp.headers["Location"] == "/login"
    assert [c for c in service.calls if c[0] != "server_info"] == []
