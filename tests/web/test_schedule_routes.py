"""Tests for the schedule pages of the admin page (routes, forms, escaping, apply)."""
import datetime

import aiohttp
import pytest

from bot.results import ActionResult
from bot.services.schedule import ScheduleDate
from bot.services.schedule_rules import Rule
from bot.ui import texts
from bot.web.auth import SessionManager
from bot.web.schedule_page import PendingView, RuleView, ScheduleOverview
from tests.notion.factories import make_event
from tests.web.test_app import FakeService, _auth, make_client

DAY = datetime.date(2026, 12, 5)
EVENT = make_event(
    event_id="2026-12-05 Rave Night", page_id="uid-1", date=DAY,
    genre="Techno", location="Hall A", notes="Bring <b>water</b>",
)
RULE = Rule(3, "location", "Hall B", 2, DAY)


class FakeSchedule:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.pending_changes: list[PendingView] = []
        self.result = ActionResult(True, "Done.")
        self.event = EVENT
        self.events = [ScheduleDate(DAY, EVENT), ScheduleDate(DAY + datetime.timedelta(7), None)]

    async def overview(self):
        return ScheduleOverview(
            True, 7, "2026-12-05", 6, {"genre": "House"},
            [RuleView(RULE, "Rave Night · 05.12.2026")],
            self.events, self.events, len(self.pending_changes),
        )

    async def has_pending(self):
        return bool(self.pending_changes)

    async def set_defaults(self, genre, location):
        self.calls.append(("defaults", genre, location))
        return self.result

    async def add_rule(self, field, value, every, start):
        self.calls.append(("add", field, value, every, start))
        return self.result

    async def edit_rule(self, rule_id, field, value, every, start):
        self.calls.append(("edit", rule_id, field, value, every, start))
        return self.result

    async def remove_rule(self, rule_id):
        self.calls.append(("remove", rule_id))
        return self.result

    async def get_rule(self, rule_id):
        return RuleView(RULE, "Rave Night · 05.12.2026") if rule_id == 3 else None

    async def anchor_options(self, current=None):
        return [("Rave Night · 05.12.2026", "2026-12-05")]

    async def get_event(self, uid):
        return self.event if uid == "uid-1" else None

    async def set_event(self, uid, genre, location, note):
        self.calls.append(("event", uid, genre, location, note))
        return self.result

    async def pending(self):
        return self.pending_changes

    async def apply(self, choice, picks, shown):
        self.calls.append(("apply", choice, picks, shown))
        return self.result


def _service() -> FakeService:
    service = FakeService()
    service.schedule = FakeSchedule()
    return service


def _form(csrf: str, **fields) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("csrf", csrf)
    for key, value in fields.items():
        for item in value if isinstance(value, list) else [value]:
            form.add_field(key, item)
    return form


async def test_schedule_page_lists_defaults_rules_and_events() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = _service()
    service.schedule.pending_changes = [PendingView("uid-1", "genre", "x")]
    async with make_client(service, sessions) as client:
        text = await (await client.get("/schedule", headers=headers)).text()
        dash = await (await client.get("/", headers=headers)).text()
    assert 'value="House"' in text
    assert "#3" in text and "Hall B" in text and "/schedule/rules/3/edit" in text
    assert "/schedule/rules/3/remove" in text
    assert "Techno" in text and "Hall A" in text and "/schedule/events/uid-1" in text
    assert "1 upcoming event differ from the rules" in text and "/schedule/apply" in text
    assert "Not created yet" in text
    assert 'href="/schedule"' in dash


async def test_schedule_requires_login() -> None:
    async with make_client(_service()) as client:
        resp = await client.get("/schedule", allow_redirects=False)
        post = await client.post("/schedule/defaults", data={"genre": "x"}, allow_redirects=False)
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert post.status == 303


@pytest.mark.parametrize(
    "path",
    ["/schedule/defaults", "/schedule/rules/add", "/schedule/rules/3/edit",
     "/schedule/rules/3/remove", "/schedule/events/uid-1", "/schedule/apply"],
)
async def test_post_without_csrf_refused(path) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        resp = await client.post(path, headers=headers, data=_form("wrong"))
    assert resp.status == 403
    assert service.schedule.calls == []


async def test_add_rule_conflict_rerenders_with_message() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    service.schedule.result = ActionResult(
        False, "This rule collides with #3 (x). Remove that rule first."
    )
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/schedule/rules/add", headers=headers,
            data=_form(csrf, field="location", value="Hall C", every="2", start="2026-12-05"),
            allow_redirects=False,
        )
        text = await resp.text()
    assert resp.status == 400 and "collides with #3" in text and 'value="Hall C"' in text


async def test_add_rule_redirects_to_apply_when_pending() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    service.schedule.pending_changes = [PendingView("uid-1", "genre", "x")]
    data = dict(field="genre", value="House", every="1", start="2026-12-05")
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/schedule/rules/add", headers=headers, data=_form(csrf, **data), allow_redirects=False
        )
        assert resp.status == 303
        assert resp.headers["Location"] == "/schedule/apply?done=rule-added"
        service.schedule.pending_changes = []
        resp = await client.post(
            "/schedule/rules/add", headers=headers, data=_form(csrf, **data), allow_redirects=False
        )
        assert resp.status == 303 and resp.headers["Location"] == "/schedule?done=rule-added"
        page = await (await client.get(resp.headers["Location"], headers=headers)).text()
        assert "The rule was added." in page
    assert service.schedule.calls[0] == ("add", "genre", "House", "1", "2026-12-05")


async def test_apply_selected_applies_only_picks() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    service.schedule.pending_changes = [
        PendingView("uid-1", "genre", "a"), PendingView("uid-2", "location", "b"),
    ]
    async with make_client(service, sessions) as client:
        page = await (await client.get("/schedule/apply", headers=headers)).text()
        resp = await client.post(
            "/schedule/apply", headers=headers,
            data=_form(csrf, choice="selected", pick=["uid-2:location"],
                       shown=["uid-1:genre", "uid-2:location"]),
            allow_redirects=False,
        )
    assert "Apply to all" in page and "Apply selected" in page and "Keep all as they are" in page
    assert resp.status == 303 and resp.headers["Location"].endswith("done=applied")
    assert service.schedule.calls == [
        ("apply", "selected", ["uid-2:location"], ["uid-1:genre", "uid-2:location"])
    ]


async def test_event_edit_empty_field_clears() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        form = await (await client.get("/schedule/events/uid-1", headers=headers)).text()
        resp = await client.post(
            "/schedule/events/uid-1", headers=headers,
            data=_form(csrf, genre="", location="Hall A", note=""),
            allow_redirects=False,
        )
        missing = await client.get("/schedule/events/nope", headers=headers)
    assert 'value="Techno"' in form and "Bring &lt;b&gt;water" in form
    assert resp.status == 303 and resp.headers["Location"] == "/schedule?done=event-saved"
    assert service.schedule.calls == [("event", "uid-1", "", "Hall A", "")]
    assert missing.status == 404


async def test_values_are_html_escaped() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, _ = _auth(sessions)
    service = _service()
    service.schedule.event = make_event(
        event_id="2026-12-05 Rave Night", page_id="uid-1", date=DAY,
        genre="<script>alert(1)</script>", location='"><img src=x>',
    )
    service.schedule.events = [ScheduleDate(DAY, service.schedule.event)]
    async with make_client(service, sessions) as client:
        text = await (await client.get("/schedule", headers=headers)).text()
        form = await (await client.get("/schedule/events/uid-1", headers=headers)).text()
    for page in (text, form):
        assert "<script>alert" not in page and "<img src=x>" not in page
    assert "&lt;script&gt;" in text


async def test_rule_edit_and_remove_flow() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        edit = await (await client.get("/schedule/rules/3/edit", headers=headers)).text()
        remove = await (await client.get("/schedule/rules/3/remove", headers=headers)).text()
        gone = await client.get("/schedule/rules/9/edit", headers=headers)
        await client.post(
            "/schedule/rules/3/edit", headers=headers,
            data=_form(csrf, value="Hall X", every="3", start="2026-12-05"),
        )
        await client.post("/schedule/rules/3/remove", headers=headers, data=_form(csrf))
    assert 'value="Hall B"' in edit and "Remove rule #3" in remove
    assert gone.status == 404
    assert service.schedule.calls == [
        ("edit", 3, "location", "Hall X", "3", "2026-12-05"), ("remove", 3),
    ]


@pytest.mark.parametrize(
    ("path", "fields"),
    [
        ("/schedule/defaults", {"genre": "x", "location": "y"}),
        ("/schedule/rules/add",
         {"field": "genre", "value": "x", "every": "1", "start": "2026-12-05"}),
        ("/schedule/rules/3/edit", {"value": "x", "every": "1", "start": "2026-12-05"}),
        ("/schedule/rules/3/remove", {}),
        ("/schedule/events/uid-1", {"genre": "x", "location": "", "note": ""}),
        ("/schedule/apply", {"choice": "all"}),
    ],
)
async def test_schedule_writes_refused_in_maintenance(path, fields) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    service.maintenance = lambda: texts.MAINTENANCE
    async with make_client(service, sessions) as client:
        resp = await client.post(path, headers=headers, data=_form(csrf, **fields))
        assert resp.status == 503
        assert texts.MAINTENANCE in await resp.text()
    assert service.schedule.calls == []


async def test_remove_and_defaults_redirect_and_keep_does_not_loop() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    async with make_client(service, sessions) as client:
        gone = await client.post(
            "/schedule/rules/3/remove", headers=headers, data=_form(csrf), allow_redirects=False
        )
        service.schedule.pending_changes = [PendingView("uid-1", "genre", "x")]
        keep = await client.post(
            "/schedule/apply", headers=headers,
            data=_form(csrf, choice="keep", shown=["uid-1:genre"]), allow_redirects=False,
        )
        unknown = await client.get("/schedule?done=%3Cb%3E", headers=headers)
        page = await unknown.text()
    assert gone.headers["Location"] == "/schedule?done=rule-removed"
    assert keep.status == 303 and keep.headers["Location"] == "/schedule?done=kept"
    assert unknown.status == 200 and "%3Cb%3E" not in page and 'class="msg ok' not in page


async def test_apply_accepts_a_body_with_104_changes_and_other_posts_stay_small() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = _service()
    shown = [f"{'0123456789abcdef' * 2}-{i // 2}:{'genre' if i % 2 else 'location'}"
             for i in range(104)]
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/schedule/apply", headers=headers,
            data=_form(csrf, choice="all", shown=shown, pick=shown), allow_redirects=False,
        )
        big = await client.post(
            "/schedule/defaults", headers=headers,
            data=_form(csrf, genre="x" * 5000), allow_redirects=False,
        )
    assert resp.status == 303
    assert service.schedule.calls[0][3] == shown
    assert big.status == 413


@pytest.mark.parametrize(
    ("path", "fields"),
    [
        ("/schedule/defaults", {"genre": "x", "location": "y"}),
        ("/schedule/rules/add",
         {"field": "genre", "value": "x", "every": "1", "start": "2026-12-05"}),
        ("/schedule/rules/3/edit", {"value": "x", "every": "1", "start": "2026-12-05"}),
        ("/schedule/rules/3/remove", {}),
        ("/schedule/events/uid-1", {"genre": "x", "location": "", "note": ""}),
        ("/schedule/apply", {"choice": "all"}),
    ],
)
async def test_schedule_posts_without_service_show_not_ready(path, fields) -> None:
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    service = FakeService()
    service.schedule = None
    async with make_client(service, sessions) as client:
        resp = await client.post(path, headers=headers, data=_form(csrf, **fields))
        assert resp.status == 503
        assert texts.SCHEDULE_NOT_READY in await resp.text()
