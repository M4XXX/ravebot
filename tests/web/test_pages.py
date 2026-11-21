"""Tests for bot/web/pages.py."""
import datetime
import html

from bot.db.issues import IssueRow
from bot.db.outbox import OutboxStats
from bot.web.pages import (
    render_backup_import,
    render_confirm_issue,
    render_confirm_rebuild,
    render_confirm_recreate,
    render_confirm_remove,
    render_dashboard,
    render_login,
    render_notice,
)
from bot.web.service import ActionResult, AdminRow, ServerInfo, SyncStatus, UserInfo

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
    unknown = render_confirm_remove(_row(7, None, on_server=False), "tok")
    assert "Remove Unknown user (7)?" in unknown
    assert "<b>Unknown user</b>" in unknown and "not on the server" in unknown


def test_message_styles() -> None:
    assert 'class="msg ok"' in render_dashboard(SERVER, [], "t", ActionResult(True, "Added X."))
    assert 'class="msg err"' in render_dashboard(SERVER, [], "t", ActionResult(False, "No."))


# --- data and backup, Notion sync (Task 17) ---

NOW = datetime.datetime(2026, 11, 20, 12, 30, tzinfo=datetime.UTC)


def _issue(issue_id=7, kind="conflict", summary="Moon changed", payload=None):
    return IssueRow(
        id=issue_id, kind=kind, entity="dj", entity_key="42", summary=summary,
        pg_payload={"dj_name": "Moon"}, notion_payload=payload or {"dj_name": "Moon2"},
        created_at=NOW, resolved_at=None, resolution=None,
    )


def _status(enabled=True, issues=(), **kw):
    values = {
        "enabled": enabled, "last_run": NOW, "last_error": None,
        "outbox": OutboxStats(0, None, None), "issues": list(issues),
        "problem": None, "maintenance": None,
    }
    values.update(kw)
    return SyncStatus(**values)


def test_backup_card_always_shown() -> None:
    for sync in (None, _status(enabled=False), _status()):
        page = render_dashboard(SERVER, [], "tok", sync=sync)
        assert "Data and backup" in page
        assert 'href="/backup/export"' in page and 'href="/backup/import"' in page


def test_notion_card_hidden_when_disabled() -> None:
    for sync in (None, _status(enabled=False, issues=[_issue()])):
        page = render_dashboard(SERVER, [], "tok", sync=sync)
        assert "Notion sync" not in page and "/sync/" not in page
    page = render_dashboard(SERVER, [], "tok", sync=_status())
    assert "Notion sync" in page and 'href="/sync/rebuild"' in page
    assert 'href="/sync/resync"' in page


def test_retry_card_shown_while_notion_not_connected() -> None:
    sync = _status(enabled=False, problem="Databases <missing>", can_retry=True)
    page = render_dashboard(SERVER, [], "tok", sync=sync)
    assert 'href="/sync/retry-import"' in page
    assert "Databases &lt;missing&gt;" in page and "<missing>" not in page
    assert 'href="/sync/rebuild"' not in page and 'href="/sync/resync"' not in page
    page = render_dashboard(SERVER, [], "tok", sync=_status(enabled=False, problem="x"))
    assert "/sync/" not in page


def test_notion_card_status_and_issue_buttons() -> None:
    sync = _status(
        issues=[_issue(), _issue(8, "warning", "Deleted in Notion")],
        outbox=OutboxStats(4, NOW, "rate limited"), last_error="read failed",
    )
    page = render_dashboard(SERVER, [], "tok", sync=sync)
    assert "2026-11-20 12:30 UTC" in page
    assert "rate limited" in page and "read failed" in page
    assert 'href="/sync/issues/7?choice=postgres"' in page
    assert 'href="/sync/issues/7?choice=notion"' in page
    assert 'href="/sync/issues/8?choice=resynced"' in page
    assert "/sync/issues/8?choice=notion" not in page
    assert "Keep Postgres" in page and "Keep Notion" in page
    assert "Resync Postgres to Notion" in page
    assert "/sync/retry-import" not in page
    assert "never" in render_dashboard(SERVER, [], "t", sync=_status(last_run=None))


def test_notion_card_escapes_notion_text() -> None:
    sync = _status(
        issues=[_issue(summary=XSS_SCRIPT)], problem=XSS_IMG, last_error=XSS_SCRIPT,
        outbox=OutboxStats(1, NOW, XSS_IMG), maintenance=XSS_IMG,
    )
    page = render_dashboard(SERVER, [], "tok", sync=sync)
    _assert_escaped(page, XSS_SCRIPT)
    _assert_escaped(page, XSS_IMG)
    assert 'href="/sync/retry-import"' in page


def test_maintenance_shown_without_notion() -> None:
    page = render_dashboard(
        SERVER, [], "tok", sync=_status(enabled=False, maintenance="A rebuild is running.")
    )
    assert "A rebuild is running." in page


def test_confirm_issue_escapes_payloads() -> None:
    issue = _issue(summary=XSS_SCRIPT, payload={"dj_name": XSS_IMG, XSS_SCRIPT: 1})
    page = render_confirm_issue(issue, "notion", "tok")
    _assert_escaped(page, XSS_SCRIPT)
    _assert_escaped(page, XSS_IMG)
    assert 'action="/sync/issues/7"' in page and 'name="choice" value="notion"' in page
    assert 'name="csrf" value="tok"' in page


def test_confirm_rebuild_and_import_forms() -> None:
    page = render_confirm_rebuild("tok")
    assert 'action="/sync/rebuild"' in page and "safety backup" in page
    page = render_backup_import("tok")
    assert 'action="/backup/import"' in page and 'enctype="multipart/form-data"' in page
    assert 'type="file" name="backup"' in page and 'name="csrf" value="tok"' in page


def test_recreate_confirm_warns_about_upgrade_import() -> None:
    page = render_confirm_recreate("gone", "tok")
    assert "marks the one-time import from Notion as done for good" in page
    assert "zero events" not in page


def test_recreate_confirm_strong_warning_while_import_pending() -> None:
    page = render_confirm_recreate("gone", "tok", import_pending=True)
    assert "has not run yet" in page and "zero events" in page
    assert "retry the import instead" in page and "DJ outreach" in page


def test_new_pages_have_no_em_dash_or_script() -> None:
    sync = _status(issues=[_issue(), _issue(8, "warning")], problem="p", maintenance="m")
    for page in (
        render_dashboard(SERVER, [], "tok", sync=sync),
        render_confirm_issue(_issue(), "postgres", "tok"),
        render_confirm_issue(_issue(8, "warning"), "resynced", "tok"),
        render_confirm_rebuild("tok"),
        render_backup_import("tok"),
    ):
        assert chr(0x2014) not in page
        assert "<script" not in page.lower()


def test_missing_databases_card_says_paused_only_when_paused() -> None:
    """M3: after the upgrade import, missing databases do not pause booking."""
    page = render_dashboard(SERVER, [], "tok", notion_problem="gone")
    assert "Notion databases not found" in page
    assert "paused" not in page
    assert "Booking and the lineup keep working" in page
    page = render_dashboard(SERVER, [], "tok", notion_problem="gone", notion_paused=True)
    assert "booking and edits are paused" in page


def _schedule_overview(rules=()):
    from bot.web.schedule_page import ScheduleOverview

    return ScheduleOverview(
        enabled=True, interval_days=7, first_date=None, lookahead=4,
        defaults={}, rules=list(rules), dates=[], anchors=[], pending=0,
    )


def test_rule_form_fields_have_labels_and_hints() -> None:
    from bot.web.pages import render_schedule

    page = render_schedule(_schedule_overview(), [("Rave Night · 05.12.2026", "2026-12-05")], "t")
    for label in ("Sets", "Value", "Every how many events", "Starting with"):
        assert f"<span>{label}</span>" in page
    assert "1 = every event, 2 = every second event" in page
    assert "The first scheduled event the rule applies to." in page


def test_add_rule_form_is_a_separate_block_after_the_rule_list() -> None:
    from bot.services.schedule_rules import Rule
    from bot.web.pages import render_schedule
    from bot.web.schedule_page import RuleView

    view = RuleView(Rule(1, "location", "Hall B", 2, datetime.date(2026, 12, 5)), "Rave Night")
    page = render_schedule(_schedule_overview([view]), [], "t")
    listing = page.index("#1 Location: Hall B")
    block = page.index('<div class="sub"><h4>Add a rule</h4>')
    assert listing < block < page.index('action="/schedule/rules/add"')


def test_edit_rule_form_uses_the_same_labels() -> None:
    from bot.services.schedule_rules import Rule
    from bot.web.pages import render_edit_rule
    from bot.web.schedule_page import RuleView

    view = RuleView(Rule(1, "genre", "Techno", 2, datetime.date(2026, 12, 5)), "Rave Night")
    page = render_edit_rule(view, [("Rave Night · 05.12.2026", "2026-12-05")], "t")
    for label in ("Value", "Every how many events", "Starting with"):
        assert f"<span>{label}</span>" in page
    assert 'value="Techno"' in page and 'value="2"' in page
    assert '<option value="2026-12-05" selected>' in page
