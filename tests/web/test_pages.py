"""Tests for bot/web/pages.py."""
import html

from bot.web.pages import render_confirm_remove, render_dashboard, render_login, render_notice
from bot.web.service import ActionResult, AdminRow, ServerInfo, UserInfo

XSS_SCRIPT = "<script>alert(1)</script>"
XSS_IMG = '"><img src=x onerror=alert(1)>'
CDN = "https://cdn.discordapp.com/avatars/1/abc.png?size=64"
SERVER = ServerInfo(111111111111111111, "Rave Galaxy", None)


def _row(
    uid=123456789012345678, name: str | None = "Luna", avatar=None, on_server=True, owner=False
):
    return AdminRow(UserInfo(uid, name, avatar, on_server), owner)


def _assert_escaped(page: str, value: str) -> None:
    assert value not in page
    assert html.escape(value, quote=True) in page


def test_dashboard_escapes_names() -> None:
    rows = [_row(1, XSS_SCRIPT), _row(2, XSS_IMG)]
    page = render_dashboard(SERVER, rows, "tok")
    _assert_escaped(page, XSS_SCRIPT)
    _assert_escaped(page, XSS_IMG)
    assert "onerror=alert(1)>" not in page.replace("&gt;", "")  # no live attribute


def test_dashboard_escapes_server_name_csrf_and_message() -> None:
    server = ServerInfo(1, XSS_SCRIPT, None)
    page = render_dashboard(server, [], XSS_IMG, ActionResult(False, XSS_SCRIPT + XSS_IMG))
    _assert_escaped(page, XSS_SCRIPT)
    _assert_escaped(page, XSS_IMG)


def test_login_escapes_error() -> None:
    _assert_escaped(render_login(XSS_IMG), XSS_IMG)
    _assert_escaped(render_notice(XSS_SCRIPT), XSS_SCRIPT)


def test_confirm_escapes_name_and_csrf() -> None:
    page = render_confirm_remove(_row(5, XSS_IMG), XSS_SCRIPT)
    _assert_escaped(page, XSS_IMG)
    _assert_escaped(page, XSS_SCRIPT)
    page = render_confirm_remove(_row(5, XSS_SCRIPT), "tok")
    _assert_escaped(page, XSS_SCRIPT)


def test_avatar_only_from_discord_cdn() -> None:
    bad = "https://evil.example/cdn.discordapp.com/a.png"
    page = render_dashboard(SERVER, [_row(1, avatar=bad), _row(2, avatar=CDN)], "tok")
    assert "evil.example" not in page
    assert html.escape(CDN, quote=True) in page
    assert page.count("<img") == 1
    for url in ("http://cdn.discordapp.com/a.png", "javascript:alert(1)", "//cdn.discordapp.com/x"):
        assert "<img" not in render_dashboard(SERVER, [_row(1, avatar=url)], "tok")


def test_server_icon_only_from_discord_cdn() -> None:
    page = render_dashboard(ServerInfo(1, "S", "https://example.com/i.png"), [], "tok")
    assert "example.com" not in page and "<img" not in page
    page = render_dashboard(ServerInfo(1, "S", CDN), [], "tok")
    assert "<img" in page


def test_remove_link_only_for_added_admins() -> None:
    rows = [_row(111111111111111111, "Boss", owner=True), _row(222222222222222222, "Luna")]
    page = render_dashboard(SERVER, rows, "tok")
    assert 'href="/admins/remove?id=222222222222222222"' in page
    assert "/admins/remove?id=111111111111111111" not in page
    assert page.count(">Remove<") == 1
    assert "Owner (environment key)" in page
    assert "Added here" in page


def test_unknown_user_and_not_on_server() -> None:
    page = render_dashboard(SERVER, [_row(7, None, on_server=False)], "tok")
    assert "Unknown user" in page
    assert "not on the server" in page
    assert "not on the server" not in render_dashboard(SERVER, [_row(7)], "tok")


def test_block_order_and_count() -> None:
    page = render_dashboard(SERVER, [_row(1), _row(2)], "tok")
    server = page.index(">Server<")
    add = page.index(">Add an admin<")
    admins = page.index(">Admins with bot commands (2)<")
    assert server < add < admins


def test_dashboard_without_server() -> None:
    page = render_dashboard(None, [], "tok")
    assert ">Server<" in page
    assert "Admins with bot commands (0)" in page


def test_title_uses_nbsp_and_no_script() -> None:
    for page in (
        render_login(),
        render_dashboard(SERVER, [_row(1)], "tok"),
        render_confirm_remove(_row(1), "tok"),
        render_notice("x"),
    ):
        assert "Rave<span>Bot</span>&nbsp;Admin&nbsp;Page" in page
        assert "<title>RaveBot Admin Page</title>" in page
        assert "<script" not in page.lower()
        assert " on" + "click" not in page
        assert chr(0x2014) not in page
        assert "line-height:1.6" in page


def test_forms_carry_csrf_and_fields() -> None:
    page = render_dashboard(SERVER, [], "tok123")
    assert 'action="/admins/add"' in page
    assert 'name="id"' in page
    assert 'action="/logout"' in page
    assert page.count('name="csrf" value="tok123"') == 2
    login = render_login()
    assert 'action="/login"' in login and 'name="password"' in login
    assert 'type="password"' in login and "csrf" not in login


def test_confirm_page() -> None:
    page = render_confirm_remove(_row(222222222222222222, "Luna"), "tok")
    assert "Remove Luna?" in page
    assert 'action="/admins/remove"' in page
    assert 'name="id" value="222222222222222222"' in page
    assert 'name="csrf" value="tok"' in page
    assert 'href="/"' in page and "Cancel" in page
    assert "Remove 7?" in render_confirm_remove(_row(7, None), "tok")


def test_message_styles() -> None:
    assert 'class="msg ok"' in render_dashboard(SERVER, [], "t", ActionResult(True, "Added X."))
    assert 'class="msg err"' in render_dashboard(SERVER, [], "t", ActionResult(False, "No."))
