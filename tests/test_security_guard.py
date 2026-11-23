"""Guard tests: every outside-facing entry point is admin-gated, and new ones must be classified.

A new slash command, admin page route or view fails one of these tests until it is gated
or added to the matching list here on purpose.
"""
import ast
import importlib
import inspect
import pathlib
import pkgutil
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import disnake
import pytest
from aiohttp.test_utils import TestClient, TestServer
from disnake.ext import commands

import bot
from bot.cogs.admins_cog import AdminsCog
from bot.config import Config
from bot.context import BotContext
from bot.web.app import create_app
from bot.web.auth import COOKIE_NAME, SessionManager
from tests.web.test_app import PASSWORD, FakeService

ROOT = pathlib.Path(bot.__file__).parent
ADMIN = 1
STRANGER = 999


def _modules(package: str):
    pkg = importlib.import_module(package)
    for info in pkgutil.walk_packages(pkg.__path__, package + "."):
        yield importlib.import_module(info.name)


def _ctx() -> BotContext:
    cfg = MagicMock(spec=Config)
    cfg.admin_user_ids = frozenset({ADMIN})
    return BotContext(config=cfg, db=MagicMock())


def _inter(user_id: int) -> MagicMock:
    inter = MagicMock()
    inter.author.id = user_id
    inter.guild_id = 5
    inter.response.send_message = AsyncMock()
    return inter


def _cog_classes() -> list[type[commands.Cog]]:
    found = []
    for module in _modules("bot.cogs"):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                found.append(cls)
    return found


def _build(cls: type[commands.Cog], ctx: BotContext) -> commands.Cog:
    if cls is AdminsCog:
        return cls(ctx, MagicMock())
    return cls(ctx)


# --- slash commands -------------------------------------------------------------------


def test_every_cog_with_slash_commands_has_its_own_admin_check() -> None:
    ctx = _ctx()
    with_commands = 0
    for cls in _cog_classes():
        cog = _build(cls, ctx)
        if not (cog.get_slash_commands() or cog.get_user_commands() or cog.get_message_commands()):
            continue
        with_commands += 1
        assert not cog.get_user_commands(), f"{cls.__name__}: user commands need a review"
        assert not cog.get_message_commands(), f"{cls.__name__}: message commands need a review"
        assert cls.cog_slash_command_check is not commands.Cog.cog_slash_command_check, (
            f"{cls.__name__} has slash commands but no cog_slash_command_check"
        )
    assert with_commands >= 4


@pytest.mark.asyncio
async def test_a_stranger_fails_every_cog_check() -> None:
    ctx = _ctx()
    for cls in _cog_classes():
        cog = _build(cls, ctx)
        if not cog.get_slash_commands():
            continue
        try:
            result = await cog.cog_slash_command_check(_inter(STRANGER))
        except commands.CheckFailure:
            continue  # a refusal raised as the owner check does
        assert result is False, f"{cls.__name__} lets a non-admin run its commands"


@pytest.mark.asyncio
async def test_only_owners_pass_the_admins_command_check() -> None:
    ctx = _ctx()
    ctx.admins = MagicMock()
    ctx.admins.is_admin.return_value = True
    ctx.admins.is_owner.return_value = False
    ctx.admins.ensure_loaded = AsyncMock()
    cog = AdminsCog(ctx, MagicMock())
    inter = _inter(ADMIN)
    with pytest.raises(commands.CheckFailure):
        await cog.cog_slash_command_check(inter)


def test_every_slash_command_is_server_only() -> None:
    ctx = _ctx()
    for cls in _cog_classes():
        for command in _build(cls, ctx).get_slash_commands():
            contexts = command.body.contexts
            assert contexts is not None and not contexts.private_channel and not (
                contexts.bot_dm
            ), f"/{command.qualified_name} must be limited to servers"


# --- the admin page --------------------------------------------------------------------


@asynccontextmanager
async def _web_client():
    app = create_app(
        FakeService(),
        PASSWORD,
        sessions=SessionManager(secret=b"k" * 32),
        fail_delay=0,
    )
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    try:
        yield app, client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_every_admin_page_route_needs_a_login() -> None:
    async with _web_client() as (app, client):
        checked = 0
        for route in app.router.routes():
            info = route.resource.get_info()
            raw = info.get("path") or info.get("formatter")
            if raw is None or raw == "/login" or route.method in ("HEAD", "*"):
                continue
            path = raw.replace("{id}", "1").replace("{uid}", "x")
            resp = await client.request(
                route.method, path, data={"csrf": "x"}, allow_redirects=False
            )
            assert resp.status == 303 and resp.headers["Location"] == "/login", (
                f"{route.method} {raw} does not require a login ({resp.status})"
            )
            checked += 1
        assert checked >= 30


@pytest.mark.asyncio
async def test_a_session_without_csrf_cannot_post() -> None:
    sessions = SessionManager(secret=b"k" * 32)
    app = create_app(FakeService(), PASSWORD, sessions=sessions, fail_delay=0)
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    try:
        token, _session = sessions.create()
        resp = await client.post(
            "/admins/add",
            data={"id": "1"},
            headers={"Cookie": f"{COOKIE_NAME}={token}"},
            allow_redirects=False,
        )
        assert resp.status == 403
    finally:
        await client.close()


# --- views and popups ------------------------------------------------------------------

# Views that only admins may use: each must check is_admin itself (the popup or button
# can be reached by a crafted custom id).
ADMIN_VIEWS = {
    "bot.cogs.admin": {"_ConfirmDelete", "_ConfirmCancelEvent"},
    "bot.ui.log_reply": {"LogReplyView", "ReplyModal"},
    "bot.ui.management_lineup": {"ManagementInfoView"},
    "bot.ui.schedule_views": {"ApplyChangesView"},
    "bot.ui.setup_rules": {"RuleModal"},
    "bot.ui.setup_wizard": {"SetupWizardView", "ValuesModal"},
}
# Views for DJs and everyone: they act on the user who presses them (inter.author) or are
# bound to one user by GatedView, and show no data of other people.
USER_VIEWS = {
    "bot.ui.dj_profile": {"OnboardingIntroView", "QuestionView", "SummaryView", "OverviewView"},
    "bot.ui.gates": {"GatedView"},
    "bot.ui.views": {
        "FlowView", "FallbackMenuView", "LineupEventPickView", "LineupPostView",
        "EventSelectionView", "SlotSelectionView", "_SlotConfirmView", "BookingConfirmView",
        "CancellationView", "CancelConfirmView", "OutreachView",
    },
}
# Names whose admin check lives outside the class body (a helper the class delegates to).
CHECKED_ELSEWHERE = {"RuleModal": "on_rule_submitted in bot.ui.setup_wizard checks is_admin"}


def _ui_classes():
    for module in _modules("bot"):
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            if issubclass(cls, (disnake.ui.View, disnake.ui.Modal)):
                yield module.__name__, name, cls


def test_every_view_and_modal_is_classified() -> None:
    known = {
        (m, n) for group in (ADMIN_VIEWS, USER_VIEWS) for m, names in group.items() for n in names
    }
    seen = {(m, n) for m, n, _ in _ui_classes()}
    assert seen - known == set(), f"classify these views: {sorted(seen - known)}"
    assert known - seen == set(), f"stale entries: {sorted(known - seen)}"


def test_admin_views_check_the_admin_list() -> None:
    for module, name, cls in _ui_classes():
        if name not in ADMIN_VIEWS.get(module, set()) or name in CHECKED_ELSEWHERE:
            continue
        assert "is_admin" in inspect.getsource(cls), f"{module}.{name} never calls is_admin"


def test_user_views_never_read_another_persons_data_by_id() -> None:
    """A user view must not look up a DJ by an id taken from a button or custom id."""
    for module, name, cls in _ui_classes():
        if name not in USER_VIEWS.get(module, set()):
            continue
        source = inspect.getsource(cls)
        assert "custom_id.split" not in source, f"{module}.{name} reads an id from a custom id"


# --- answers ---------------------------------------------------------------------------

ANSWER_MODULES = [
    "cogs/admin.py", "cogs/schedule_cog.py", "cogs/setup.py", "cogs/admins_cog.py",
    "ui/management_lineup.py", "ui/schedule_views.py", "ui/log_reply.py", "ui/setup_rules.py",
]
# A public answer is allowed only here: the setup wizard message must stay editable after
# restarts, which an ephemeral message does not.
PUBLIC_ANSWER_ALLOWED = {("cogs/setup.py", "SETUP_ALREADY_ACTIVE")}


def test_admin_command_answers_are_private() -> None:
    offenders = []
    for rel in ANSWER_MODULES:
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = ast.unparse(node.func.value)
            if node.func.attr not in ("send_message", "send") or not (
                "response" in target or "followup" in target
            ):
                continue
            if any(k.arg == "ephemeral" for k in node.keywords):
                continue
            text = ast.unparse(node)
            if any(rel == r and marker in text for r, marker in PUBLIC_ANSWER_ALLOWED):
                continue
            offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"public answers in admin code: {offenders}"


# --- public posts ------------------------------------------------------------------------


def test_the_public_lineup_shows_dj_names_only() -> None:
    import datetime

    from bot.ui.embeds import LineupUpdateEmbed
    from tests.notion.factories import make_event, make_slot

    discord_id = 123456789012345678
    event = make_event(
        date=datetime.date(2027, 1, 2),
        slots=[make_slot(
            1, dj_page_id="dj1", dj_name="DJ Felix <@777>", discord_id=discord_id
        )],
    )
    embed = LineupUpdateEmbed(event)
    text = " ".join([str(embed.title), str(embed.description)] + [
        f"{f.name} {f.value}" for f in embed.fields
    ])
    assert "DJ Felix" in text
    assert str(discord_id) not in text
    assert "<@777>" not in text  # a mention in a name is broken up
