"""Tests for the Announcements page of the admin page (routes, service, forms, escaping).

The routes run against the real AdminPageService announcement methods; the database
layer (config values, announcement rules, events) is an in-memory fake.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import disnake
import pytest

from bot.context import BotContext
from bot.db import announcements as db_ann
from bot.db import config as db_config
from bot.db import events as db_events
from bot.db.announcements import AnnouncementRule
from bot.notion.models import EventInfo, SlotInfo
from bot.services import announcement_settings as ann_settings
from bot.services.admin_registry import AdminRegistry
from bot.services.announcement_template import DEFAULT_TEMPLATES, GENERAL, START
from bot.timezone import local_today
from bot.ui import texts
from bot.web.auth import SessionManager
from bot.web.service import AdminPageService
from tests.web.test_app import HEADERS, XSS_IMG, XSS_SCRIPT, FakeService, _auth, make_client

GUILD_ID = 999999999999999999
CHANNEL_ID = 444444444444444444
OTHER_CHANNEL = 555555555555555555
VOICE_CHANNEL = 666666666666666666
NO_PERM_CHANNEL = 777777777777777777
GENERAL_KEY = ann_settings.ANNOUNCE_GENERAL_TEMPLATE_KEY
SINCE_KEY = ann_settings.ANNOUNCE_GENERAL_SINCE_KEY
_ANN_METHODS = {
    "announcements",
    "save_announcement_settings",
    "add_rule",
    "edit_rule",
    "remove_rule",
    "get_announcement_rule",
    "check_template",
    "save_template",
}


class Store:
    """In-memory stand-in for the config table, the announcement rules and the events."""

    def __init__(self) -> None:
        self.config: dict[str, str] = {}
        self.rules: dict[int, AnnouncementRule] = {}
        self.next_id = 1
        self.events: list[EventInfo] = []
        self.set_enabled_calls: list[tuple[str, bool]] = []

    async def get_value(self, db, guild_id, key):
        return self.config.get(key)

    async def set_value(self, db, guild_id, key, value):
        self.config[key] = value

    async def delete_value(self, db, guild_id, key):
        self.config.pop(key, None)

    async def list_rules(self, db, guild_id):
        return sorted(self.rules.values(), key=lambda r: (-r.days_before, r.post_time))

    async def add_rule(self, db, guild_id, days, at):
        rule = AnnouncementRule(self.next_id, days, at, datetime.datetime.now(datetime.UTC))
        self.rules[rule.id] = rule
        self.next_id += 1
        return rule

    async def update_rule(self, db, guild_id, rule_id, days, at):
        old = self.rules.get(rule_id)
        if old is None:
            return None
        self.rules[rule_id] = AnnouncementRule(rule_id, days, at, old.created_at)
        return self.rules[rule_id]

    async def delete_rule(self, db, guild_id, rule_id):
        return self.rules.pop(rule_id, None) is not None

    async def list_events(self, db, include_past=False):
        return list(self.events)


def _perms(ok: bool) -> SimpleNamespace:
    return SimpleNamespace(send_messages=ok, embed_links=ok, view_channel=ok)


def _text_channel(channel_id: int, name: str, ok: bool = True) -> MagicMock:
    channel = MagicMock(spec=disnake.TextChannel)
    channel.id = channel_id
    channel.name = name
    channel.permissions_for = MagicMock(return_value=_perms(ok))
    return channel


def _bot() -> MagicMock:
    channels = {
        CHANNEL_ID: _text_channel(CHANNEL_ID, "announcements"),
        OTHER_CHANNEL: _text_channel(OTHER_CHANNEL, "news"),
        NO_PERM_CHANNEL: _text_channel(NO_PERM_CHANNEL, "locked", ok=False),
        VOICE_CHANNEL: MagicMock(spec=disnake.VoiceChannel),
    }
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.name = "Rave Galaxy"
    guild.me = object()
    guild.get_channel = MagicMock(side_effect=channels.get)
    bot = MagicMock()
    bot.guilds = [guild]
    return bot


class AnnService(FakeService):
    """The test_app FakeService (dashboard) plus the real announcement methods."""

    def __init__(self, bot=None) -> None:
        super().__init__()
        registry = AdminRegistry(frozenset({1}), object())
        registry._loaded = True
        ctx = BotContext(config=MagicMock(), db=object(), admins=registry)
        self.real = AdminPageService(ctx, bot or _bot())

    def __getattr__(self, name: str):
        if name in _ANN_METHODS:
            return getattr(self.real, name)
        raise AttributeError(name)


@pytest.fixture
def store(monkeypatch) -> Store:
    s = Store()
    for name in ("get_value", "set_value", "delete_value"):
        monkeypatch.setattr(db_config, name, getattr(s, name))
    for name in ("list_rules", "add_rule", "update_rule", "delete_rule"):
        monkeypatch.setattr(db_ann, name, getattr(s, name))
    monkeypatch.setattr(db_events, "list_events", s.list_events)
    original = ann_settings.set_enabled

    async def spy(db, guild_id, kind, on, now=None):
        s.set_enabled_calls.append((kind, on))
        await original(db, guild_id, kind, on, now)

    monkeypatch.setattr(ann_settings, "set_enabled", spy)
    return s


def _event(name: str = "Moon Party", days: int = 3, booked: bool = True) -> EventInfo:
    slots = tuple(
        SlotInfo(f"s{i}", i, f"dj{i}" if booked else None, f"DJ {i}" if booked else None, None)
        for i in (1, 2)
    )
    return EventInfo(
        page_id="ev1",
        event_id=name,
        date=local_today() + datetime.timedelta(days=days),
        status="Planned",
        genre="House",
        notes="Bring water.",
        slot_count=2,
        start=datetime.time(21, 0),
        slot_minutes=60,
        slots=slots,
    )


def _form(csrf: str, **fields: str) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("csrf", csrf)
    for key, value in fields.items():
        form.add_field(key, value)
    return form


async def _session():
    sessions = SessionManager(secret=b"k" * 32)
    headers, csrf = _auth(sessions)
    return sessions, headers, csrf


def _textarea(text: str, kind: str) -> str:
    """The content of the template textarea of ``kind``."""
    part = text.split(f'action="/announcements/templates/{kind}/save"')[1]
    return part.split("<textarea")[1].split(">", 1)[1].split("</textarea>")[0]


# --- access ------------------------------------------------------------------------


ROUTES = [
    ("GET", "/announcements"),
    ("POST", "/announcements/settings"),
    ("POST", "/announcements/rules/add"),
    ("GET", "/announcements/rules/1/edit"),
    ("POST", "/announcements/rules/1/edit"),
    ("GET", "/announcements/rules/1/remove"),
    ("POST", "/announcements/rules/1/remove"),
    ("POST", "/announcements/templates/general/check"),
    ("POST", "/announcements/templates/start/save"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_requires_login(store, method, path) -> None:
    async with make_client(AnnService()) as client:
        resp = await client.request(method, path, data={"csrf": "x"}, allow_redirects=False)
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert store.config == {} and store.rules == {}


@pytest.mark.parametrize(
    ("path", "fields"),
    [
        ("/announcements/settings", {"channel": str(CHANNEL_ID), "general": "on"}),
        ("/announcements/rules/add", {"days": "2", "time": "18:00"}),
        ("/announcements/templates/general/save", {"text": "Hi {title}"}),
    ],
)
async def test_post_without_csrf_refused(store, path, fields) -> None:
    sessions, headers, _ = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(path, headers=headers, data=_form("wrong", **fields))
    assert resp.status == 403
    assert store.config == {} and store.rules == {} and store.set_enabled_calls == []


async def test_unknown_kind_is_not_found(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/other/save", headers=headers,
            data=_form(csrf, text="x"),
        )
    assert resp.status == 404 and store.config == {}


async def test_dashboard_has_link_next_to_booking_rules(store) -> None:
    sessions, headers, _ = await _session()
    async with make_client(AnnService(), sessions) as client:
        dash = await (await client.get("/", headers=headers)).text()
    assert 'href="/announcements"' in dash and ">Announcements</a>" in dash
    assert dash.index('href="/booking-rules"') < dash.index('href="/announcements"')


# --- the page ----------------------------------------------------------------------


async def test_page_shows_channel_switches_rules_and_both_editors(store) -> None:
    store.config[ann_settings.ANNOUNCE_CHANNEL_KEY] = str(CHANNEL_ID)
    store.config[ann_settings.ANNOUNCE_START_KEY] = "true"
    await store.add_rule(None, GUILD_ID, 3, datetime.time(18, 0))
    sessions, headers, _ = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.get("/announcements", headers=headers)
        text = await resp.text()
    assert resp.status == 200
    assert f'name="channel" value="{CHANNEL_ID}"' in text and "#announcements" in text
    general_box = text.split('name="general"')[1].split(">")[0]
    start_box = text.split('name="start"')[1].split(">")[0]
    assert "checked" not in general_box and "checked" in start_box
    assert "3 days before at 18:00" in text
    assert 'href="/announcements/rules/1/edit"' in text
    assert 'href="/announcements/rules/1/remove"' in text
    for kind in (GENERAL, START):
        assert f'formaction="/announcements/templates/{kind}/check"' in text
        assert f'action="/announcements/templates/{kind}/save"' in text
        assert _textarea(text, kind) == "\n" + DEFAULT_TEMPLATES[kind].replace(
            "&", "&amp;"
        ).replace("<", "&lt;").replace(">", "&gt;")
    for name in ("{title}", "{date}", "{lineup}", "{now_playing}", "{next_up}", "{status}"):
        assert name in text
    assert "Check and preview" in text and "Reset to default" in text
    assert "empty" in text.lower() and "default" in text


async def test_page_contains_no_script_tag_and_keeps_the_security_headers(store) -> None:
    store.events = [_event()]
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        pages = [await client.get("/announcements", headers=headers)]
        pages.append(await client.post(
            "/announcements/templates/general/check", headers=headers,
            data=_form(csrf, text="**{title}** {lineup}"),
        ))
        for resp in pages:
            body = await resp.text()
            assert resp.status == 200
            assert "<script" not in body.lower()
            for name, value in HEADERS.items():
                assert resp.headers.get(name) == value


async def test_not_ready_without_a_server(store) -> None:
    bot = MagicMock()
    bot.guilds = []
    sessions, headers, _ = await _session()
    async with make_client(AnnService(bot), sessions) as client:
        resp = await client.get("/announcements", headers=headers)
    assert resp.status == 503


# --- template check and save -------------------------------------------------------


async def test_check_and_preview_shows_ok_length_and_the_filled_preview(store) -> None:
    store.events = [_event("Moon & <Party>")]
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/check", headers=headers,
            data=_form(csrf, text="Hello {title} & friends\n{lineup}"),
        )
        text = await resp.text()
    assert resp.status == 200
    assert "The template is valid and can be posted." in text
    preview = text.split('<pre class="pv">')[1].split("</pre>")[0]
    assert preview.startswith("Hello Moon &amp; &lt;")  # Discord escaping adds a ZWSP
    assert "&amp; friends" in preview and "DJ 1" in preview and "<Party>" not in preview
    assert " / 2000 characters" in text
    assert "Moon &amp; &lt;Party&gt;" in text.split("Preview filled with")[1][:200]
    assert _textarea(text, GENERAL) == "\nHello {title} &amp; friends\n{lineup}"
    assert GENERAL_KEY not in store.config  # a check never saves


async def test_check_shows_length_of_the_filled_text(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        text = await (await client.post(
            "/announcements/templates/start/check", headers=headers,
            data=_form(csrf, text="Live: {slots} slots"),
        )).text()
    assert "Live: 4 slots" in text  # the sample event has 4 slots
    assert f"{len('Live: 4 slots')} / 2000 characters" in text
    assert "sample event" in text


async def test_check_shows_unknown_placeholder_problem(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/check", headers=headers,
            data=_form(csrf, text="Hi {titel} and {when}"),
        )
        text = await resp.text()
    assert resp.status == 200
    assert "Unknown placeholders: {titel}, {when}" in text
    assert "The template is valid" not in text
    assert _textarea(text, GENERAL) == "\nHi {titel} and {when}"


async def test_check_of_empty_text_says_the_default_is_used(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        text = await (await client.post(
            "/announcements/templates/general/check", headers=headers,
            data=_form(csrf, text="  "),
        )).text()
    assert "resets to the default template" in text
    assert "Friday Night Rave" in text  # the default template filled with the sample


async def test_check_warns_about_everyone(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        text = await (await client.post(
            "/announcements/templates/general/check", headers=headers,
            data=_form(csrf, text="@everyone {title}"),
        )).text()
    assert "@everyone will not ping." in text


async def test_save_runs_the_check_and_refuses_an_invalid_template_even_without_a_prior_check(
    store,
) -> None:
    store.config[GENERAL_KEY] = "Old {title}"
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/save", headers=headers,
            data=_form(csrf, text="New {titel}"),
        )
        text = await resp.text()
    assert resp.status == 400
    assert "The template was not saved" in text and "{titel}" in text
    assert _textarea(text, GENERAL) == "\nNew {titel}"
    assert store.config[GENERAL_KEY] == "Old {title}"


async def test_save_refuses_a_template_too_long_for_the_next_event(store) -> None:
    store.events = [_event("X" * 1990)]
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/save", headers=headers,
            data=_form(csrf, text="{title} {lineup}"),
        )
        text = await resp.text()
    assert resp.status == 400 and "Too long" in text
    assert GENERAL_KEY not in store.config


async def test_save_valid_template_stores_it_and_empty_resets_to_default(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/start/save", headers=headers, allow_redirects=False,
            data=_form(csrf, text="Live: {now_playing}"),
        )
        assert resp.status == 303
        assert resp.headers["Location"] == "/announcements?done=start-saved"
        assert store.config[ann_settings.ANNOUNCE_START_TEMPLATE_KEY] == "Live: {now_playing}"
        page = await (await client.get(resp.headers["Location"], headers=headers)).text()
        assert "The start template is saved." in page
        assert _textarea(page, START) == "\nLive: {now_playing}"

        resp = await client.post(
            "/announcements/templates/start/save", headers=headers, allow_redirects=False,
            data=_form(csrf, text=""),
        )
        assert resp.status == 303
        assert resp.headers["Location"] == "/announcements?done=start-reset"
        assert ann_settings.ANNOUNCE_START_TEMPLATE_KEY not in store.config
        page = await (await client.get(resp.headers["Location"], headers=headers)).text()
    assert "The start template is back to the default." in page


async def test_save_without_text_field_is_refused(store) -> None:
    store.config[GENERAL_KEY] = "Old {title}"
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/save", headers=headers, data=_form(csrf),
        )
    assert resp.status == 400 and store.config[GENERAL_KEY] == "Old {title}"


async def test_save_normalizes_crlf(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/save", headers=headers, allow_redirects=False,
            data=_form(csrf, text="A {title}\r\nB\rC  \r\n"),
        )
    assert resp.status == 303
    assert store.config[GENERAL_KEY] == "A {title}\nB\nC  \n"


async def test_check_normalizes_crlf(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        text = await (await client.post(
            "/announcements/templates/general/check", headers=headers,
            data=_form(csrf, text="A\r\nB"),
        )).text()
    assert "\r" not in text and _textarea(text, GENERAL) == "\nA\nB"
    assert "3 / 2000 characters" in text


async def test_template_longer_than_the_small_body_limit_is_accepted(store) -> None:
    template = "{title}\n" + ("ä" * 1500)  # about 9 KB url-encoded
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/save", headers=headers, allow_redirects=False,
            data=_form(csrf, text=template),
        )
    assert resp.status == 303 and store.config[GENERAL_KEY] == template


async def test_xss_in_template_and_event_is_escaped_everywhere(store) -> None:
    store.events = [_event(XSS_IMG)]
    store.config[GENERAL_KEY] = f"{XSS_SCRIPT} {XSS_IMG} {{title}}"
    sessions, headers, csrf = await _session()
    bodies = []
    async with make_client(AnnService(), sessions) as client:
        bodies.append(await (await client.get("/announcements", headers=headers)).text())
        bodies.append(await (await client.post(
            "/announcements/templates/general/check", headers=headers,
            data=_form(csrf, text=f"{XSS_SCRIPT}\n{XSS_IMG}\n{{title}}"),
        )).text())
        bodies.append(await (await client.post(
            "/announcements/templates/start/save", headers=headers,
            data=_form(csrf, text=f"{XSS_SCRIPT}{XSS_IMG}{{<script>x}}"),
        )).text())
        resp = await client.post(
            "/announcements/templates/general/save", headers=headers, allow_redirects=False,
            data=_form(csrf, text=f"{XSS_SCRIPT}\n{XSS_IMG}"),
        )
        assert resp.status == 303
        bodies.append(await (await client.get("/announcements", headers=headers)).text())
    for body in bodies:
        assert "<script" not in body.lower()
        assert "<img" not in body.lower()
        assert XSS_IMG not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in body


async def test_template_writes_refused_in_maintenance(store) -> None:
    service = AnnService()
    service.maintenance = lambda: texts.MAINTENANCE
    sessions, headers, csrf = await _session()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/announcements/templates/general/save", headers=headers,
            data=_form(csrf, text="Hi {title}"),
        )
        text = await resp.text()
    assert resp.status == 503 and texts.MAINTENANCE in text
    assert GENERAL_KEY not in store.config


# --- settings ----------------------------------------------------------------------


async def test_settings_save_channel_and_switch_on_creates_the_default_rule(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/settings", headers=headers, allow_redirects=False,
            data=_form(csrf, channel=f" {CHANNEL_ID} ", general="on"),
        )
        assert resp.status == 303
        assert resp.headers["Location"] == "/announcements?done=settings-rule"
        page = await (await client.get(resp.headers["Location"], headers=headers)).text()
    assert store.config[ann_settings.ANNOUNCE_CHANNEL_KEY] == str(CHANNEL_ID)
    assert store.config[ann_settings.ANNOUNCE_GENERAL_KEY] == "true"
    assert store.set_enabled_calls == [(GENERAL, True)]
    assert len(store.rules) == 1 and next(iter(store.rules.values())).days_before == 3
    assert "a default rule was created" in page
    assert "3 days before at 18:00" in page


async def test_settings_only_call_set_enabled_on_a_change(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        await client.post(
            "/announcements/settings", headers=headers, allow_redirects=False,
            data=_form(csrf, channel=str(CHANNEL_ID), general="on"),
        )
        since = store.config[SINCE_KEY]
        resp = await client.post(
            "/announcements/settings", headers=headers, allow_redirects=False,
            data=_form(csrf, channel=str(CHANNEL_ID), general="on"),
        )
        assert resp.headers["Location"] == "/announcements?done=settings-saved"
        assert store.set_enabled_calls == [(GENERAL, True)]
        assert store.config[SINCE_KEY] == since
        await client.post(
            "/announcements/settings", headers=headers, allow_redirects=False,
            data=_form(csrf, channel=str(CHANNEL_ID), start="on"),
        )
    assert store.set_enabled_calls == [(GENERAL, True), (GENERAL, False), (START, True)]
    assert store.config[ann_settings.ANNOUNCE_GENERAL_KEY] == "false"
    assert store.config[ann_settings.ANNOUNCE_START_KEY] == "true"


async def test_settings_empty_channel_clears_it(store) -> None:
    store.config[ann_settings.ANNOUNCE_CHANNEL_KEY] = str(CHANNEL_ID)
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/settings", headers=headers, allow_redirects=False,
            data=_form(csrf, channel=""),
        )
        page = await (await client.get("/announcements", headers=headers)).text()
    assert resp.status == 303
    assert ann_settings.ANNOUNCE_CHANNEL_KEY not in store.config
    assert store.set_enabled_calls == []
    assert "No channel is set" in page


async def test_settings_without_channel_field_keep_the_channel(store) -> None:
    store.config[ann_settings.ANNOUNCE_CHANNEL_KEY] = str(CHANNEL_ID)
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/settings", headers=headers, allow_redirects=False,
            data=_form(csrf, start="on"),
        )
    assert resp.status == 303
    assert store.config[ann_settings.ANNOUNCE_CHANNEL_KEY] == str(CHANNEL_ID)
    assert store.set_enabled_calls == [(START, True)]


async def test_settings_unchanged_channel_gone_still_lets_a_switch_turn_off(store) -> None:
    gone = 888888888888888888
    store.config[ann_settings.ANNOUNCE_CHANNEL_KEY] = str(gone)
    store.config[ann_settings.ANNOUNCE_GENERAL_KEY] = "true"
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/settings", headers=headers, allow_redirects=False,
            data=_form(csrf, channel=str(gone)),
        )
        other = await client.post(
            "/announcements/settings", headers=headers, data=_form(csrf, channel=str(gone + 1)),
        )
    assert resp.status == 303
    assert store.set_enabled_calls == [(GENERAL, False)]
    assert store.config[ann_settings.ANNOUNCE_CHANNEL_KEY] == str(gone)
    assert other.status == 400  # a different channel is still checked


async def test_settings_unchanged_channel_keeps_the_format_check(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/settings", headers=headers, data=_form(csrf, channel="abc"),
        )
    assert resp.status == 400


async def test_template_textarea_limit_stays_below_the_body_limit(store) -> None:
    from bot.web.app import MAX_TEMPLATE
    from bot.web.pages import TEMPLATE_MAXLENGTH

    # worst case: a 3-byte BMP character url-encodes to 9 bytes; 1 KiB for the other fields
    assert TEMPLATE_MAXLENGTH * 9 + 1024 <= MAX_TEMPLATE
    assert TEMPLATE_MAXLENGTH >= 2000


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("abc", "Enter the channel ID"),
        ("12345", "Enter the channel ID"),
        ("1" * 21, "Enter the channel ID"),
        ("-44444444444444444", "Enter the channel ID"),
        ("888888888888888888", "There is no text channel with this ID"),
        (str(VOICE_CHANNEL), "There is no text channel with this ID"),
        (str(NO_PERM_CHANNEL), "I cannot use #locked"),
    ],
)
async def test_settings_invalid_channel_refused_and_values_kept(store, raw, message) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/settings", headers=headers,
            data=_form(csrf, channel=raw, start="on"),
        )
        text = await resp.text()
    assert resp.status == 400 and message in text
    assert f'name="channel" value="{raw}"' in text
    assert "checked" in text.split('name="start"')[1].split(">")[0]
    assert store.config == {} and store.set_enabled_calls == []


async def test_settings_channel_input_is_escaped(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        text = await (await client.post(
            "/announcements/settings", headers=headers, data=_form(csrf, channel=XSS_IMG),
        )).text()
    assert XSS_IMG not in text and "<img" not in text


async def test_settings_refused_in_maintenance(store) -> None:
    service = AnnService()
    service.maintenance = lambda: texts.MAINTENANCE
    sessions, headers, csrf = await _session()
    async with make_client(service, sessions) as client:
        resp = await client.post(
            "/announcements/settings", headers=headers,
            data=_form(csrf, channel=str(CHANNEL_ID), general="on"),
        )
    assert resp.status == 503
    assert store.config == {} and store.set_enabled_calls == []


async def test_page_warns_when_a_switch_is_on_without_channel(store) -> None:
    store.config[ann_settings.ANNOUNCE_START_KEY] = "true"
    sessions, headers, _ = await _session()
    async with make_client(AnnService(), sessions) as client:
        text = await (await client.get("/announcements", headers=headers)).text()
    assert "nothing is posted" in text


# --- rules -------------------------------------------------------------------------


async def test_rule_add(store) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/rules/add", headers=headers, allow_redirects=False,
            data=_form(csrf, days="0", time="9:30"),
        )
        assert resp.status == 303
        assert resp.headers["Location"] == "/announcements?done=rule-added"
        page = await (await client.get(resp.headers["Location"], headers=headers)).text()
    rule = store.rules[1]
    assert rule.days_before == 0 and rule.post_time == datetime.time(9, 30)
    assert "The announcement rule was added." in page and "0 days before at 09:30" in page


@pytest.mark.parametrize(
    ("days", "time", "message"),
    [
        ("366", "18:00", texts.ANNOUNCE_RULE_DAYS_INVALID),
        ("-1", "18:00", texts.ANNOUNCE_RULE_DAYS_INVALID),
        ("", "18:00", texts.ANNOUNCE_RULE_DAYS_INVALID),
        ("x", "18:00", texts.ANNOUNCE_RULE_DAYS_INVALID),
        ("3", "24:00", texts.ANNOUNCE_RULE_TIME_INVALID),
        ("3", "18", texts.ANNOUNCE_RULE_TIME_INVALID),
        ("3", XSS_IMG, texts.ANNOUNCE_RULE_TIME_INVALID),
    ],
)
async def test_rule_add_invalid_keeps_values(store, days, time, message) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/rules/add", headers=headers, data=_form(csrf, days=days, time=time),
        )
        text = await resp.text()
    assert resp.status == 400 and message in text
    assert store.rules == {}
    assert XSS_IMG not in text
    escaped = time.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    assert f'name="time" value="{escaped.replace(">", "&gt;")}"' in text
    assert f'name="days" value="{days}"' in text


async def test_rule_edit(store) -> None:
    await store.add_rule(None, GUILD_ID, 3, datetime.time(18, 0))
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        page = await (await client.get("/announcements/rules/1/edit", headers=headers)).text()
        assert 'name="days" value="3"' in page and 'name="time" value="18:00"' in page
        assert 'action="/announcements/rules/1/edit"' in page
        resp = await client.post(
            "/announcements/rules/1/edit", headers=headers, allow_redirects=False,
            data=_form(csrf, days="1", time="12:15"),
        )
    assert resp.status == 303
    assert resp.headers["Location"] == "/announcements?done=rule-updated"
    assert store.rules[1].days_before == 1 and store.rules[1].post_time == datetime.time(12, 15)


async def test_rule_edit_invalid_keeps_values(store) -> None:
    await store.add_rule(None, GUILD_ID, 3, datetime.time(18, 0))
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.post(
            "/announcements/rules/1/edit", headers=headers,
            data=_form(csrf, days="400", time="7:00"),
        )
        text = await resp.text()
    assert resp.status == 400 and texts.ANNOUNCE_RULE_DAYS_INVALID in text
    assert 'name="days" value="400"' in text and 'name="time" value="7:00"' in text
    assert store.rules[1].days_before == 3


async def test_rule_remove_confirm_then_remove(store) -> None:
    await store.add_rule(None, GUILD_ID, 3, datetime.time(18, 0))
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        page = await (await client.get("/announcements/rules/1/remove", headers=headers)).text()
        assert "3 days before at 18:00" in page
        assert 'action="/announcements/rules/1/remove"' in page
        assert 1 in store.rules  # GET never removes
        resp = await client.post(
            "/announcements/rules/1/remove", headers=headers, allow_redirects=False,
            data=_form(csrf),
        )
    assert resp.status == 303
    assert resp.headers["Location"] == "/announcements?done=rule-removed"
    assert store.rules == {}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/announcements/rules/9/edit"),
        ("POST", "/announcements/rules/9/edit"),
        ("GET", "/announcements/rules/9/remove"),
        ("POST", "/announcements/rules/9/remove"),
    ],
)
async def test_missing_rule_is_404(store, method, path) -> None:
    sessions, headers, csrf = await _session()
    async with make_client(AnnService(), sessions) as client:
        resp = await client.request(
            method, path, headers=headers, data=_form(csrf, days="1", time="10:00")
        )
        text = await resp.text()
    assert resp.status == 404 and "This rule no longer exists." in text


@pytest.mark.parametrize(
    ("path", "fields"),
    [
        ("/announcements/rules/add", {"days": "1", "time": "10:00"}),
        ("/announcements/rules/1/edit", {"days": "1", "time": "10:00"}),
        ("/announcements/rules/1/remove", {}),
    ],
)
async def test_rule_writes_refused_in_maintenance(store, path, fields) -> None:
    await store.add_rule(None, GUILD_ID, 3, datetime.time(18, 0))
    service = AnnService()
    service.maintenance = lambda: texts.MAINTENANCE
    sessions, headers, csrf = await _session()
    async with make_client(service, sessions) as client:
        resp = await client.post(path, headers=headers, data=_form(csrf, **fields))
    assert resp.status == 503
    assert list(store.rules) == [1] and store.rules[1].days_before == 3
