"""Tests for bot.services.dj_conversation (repositories patched with an in-memory fake)."""
import asyncio
import copy
import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from bot.db.dj_conversations import Conversation
from bot.services import dj_conversation as svc_mod
from bot.services import logo as logo_mod
from bot.services.dj_conversation import EDIT_EXPIRY, DjConversationService, Reply, is_expired
from bot.ui import texts

GUILD = 500
A, B = 1, 2


def png_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(out, format="PNG")
    return out.getvalue()


class FakeDb:
    """In-memory stand-in for the dj_conversations and djs repositories."""

    def __init__(self) -> None:
        self.convs: dict[int, Conversation] = {}
        self.djs: dict[int, dict[str, Any]] = {}
        self.taken: set[str] = set()
        self.saves = 0
        self.insert_dj = AsyncMock(side_effect=self._insert_dj)
        self.update_dj_fields = AsyncMock(side_effect=self._update_dj_fields)
        self.dj_name_exists = AsyncMock(side_effect=self._dj_name_exists)
        self.update_active_booking = AsyncMock(side_effect=self._update_active_booking)

    async def get_conversation(self, pool, user_id):
        await asyncio.sleep(0)
        conv = self.convs.get(user_id)
        return copy.deepcopy(conv)

    async def save_conversation(self, pool, conv):
        await asyncio.sleep(0)
        self.saves += 1
        self.convs[conv.discord_user_id] = copy.deepcopy(conv)

    async def delete_conversation(self, pool, user_id):
        self.convs.pop(user_id, None)

    async def set_conversation_message(self, pool, user_id, message_id):
        if user_id in self.convs:
            self.convs[user_id].message_id = message_id

    async def get_dj_by_discord_id(self, pool, user_id):
        return self.djs.get(user_id)

    async def _dj_name_exists(self, pool, name, exclude_user_id=None):
        await asyncio.sleep(0)
        key = " ".join(name.split()).casefold()
        return key in self.taken

    async def _insert_dj(self, pool, **kw):
        await asyncio.sleep(0)
        self.taken.add(" ".join(kw["dj_name"].split()).casefold())
        self.djs[kw["discord_user_id"]] = {
            "dj_name": kw["dj_name"],
            "vrchat_name": kw["vrchat_name"],
            "logo_url": kw["logo_url"],
            "logo_image": kw.get("logo_image"),
            "stream_url_primary": kw["stream_url_primary"],
            "stream_url_backup": kw["stream_url_backup"],
            "active_booking": True,
        }

    async def _update_dj_fields(self, pool, user_id, **fields):
        row = self.djs.get(user_id)
        await asyncio.sleep(0)
        if row is None:
            return False
        if "dj_name" in fields:
            self.taken.add(" ".join(fields["dj_name"].split()).casefold())
        row.update(fields)
        return True

    async def _update_active_booking(self, pool, *, discord_user_id, active_booking):
        row = self.djs.get(discord_user_id)
        if row is None:
            return False
        row["active_booking"] = active_booking
        return True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    db = FakeDb()
    for name in (
        "get_conversation",
        "save_conversation",
        "delete_conversation",
        "set_conversation_message",
        "get_dj_by_discord_id",
        "insert_dj",
        "update_dj_fields",
        "dj_name_exists",
        "update_active_booking",
    ):
        monkeypatch.setattr(svc_mod, name, getattr(db, name))
    return db


@pytest.fixture
def role() -> MagicMock:
    return MagicMock(name="invites-role")


@pytest.fixture
def member() -> MagicMock:
    m = MagicMock()
    m.display_name = "alice"
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


@pytest.fixture
def client(member: MagicMock) -> MagicMock:
    guild = MagicMock()
    guild.id = GUILD
    guild.get_member = MagicMock(return_value=member)
    c = MagicMock()
    c.get_guild = MagicMock(return_value=guild)
    c.guilds = [guild]
    return c


@pytest.fixture
def ctx() -> MagicMock:
    c = MagicMock()
    c.db = MagicMock(name="pool")
    c.log_service = MagicMock()
    c.log_service.log_new_dj = AsyncMock()
    return c


@pytest.fixture
def sync(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(svc_mod, "sync_dj", mock)
    return mock


@pytest.fixture
def roles(monkeypatch: pytest.MonkeyPatch, role: MagicMock) -> AsyncMock:
    mock = AsyncMock(return_value=role)
    monkeypatch.setattr(svc_mod, "get_invites_role", mock)
    return mock


@pytest.fixture
def service(ctx, client, fake, sync, roles) -> DjConversationService:
    return DjConversationService(ctx, client=client)


@pytest.fixture
def download(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=png_bytes())
    monkeypatch.setattr(logo_mod, "download_attachment", mock)
    return mock


def msg(user: int, content: str = "", attachments: int = 0) -> Any:
    atts = [SimpleNamespace(url="https://cdn.discordapp.com/a.png", size=10)] * attachments
    return SimpleNamespace(author=SimpleNamespace(id=user), content=content, attachments=atts)


async def onboard_to(service: DjConversationService, user: int, step: str) -> None:
    await service.start_onboarding(user, GUILD)
    answers = {"name": "Neon Fox", "vrchat": "foxvr", "link": "twitch.tv/neon"}
    for current in ("name", "vrchat", "logo", "link", "alt_link"):
        if current == step:
            return
        if current in ("logo", "alt_link"):
            await service.record_message_id(user, 77)
            await service.skip(user, 77)
        else:
            await service.handle_message(msg(user, answers[current]))


# --- happy path and start ---


async def test_happy_path_all_steps_ends_in_summary_with_image(service, fake, download):
    r = await service.start_onboarding(A, GUILD)
    assert (r.kind, r.step, r.text, r.allow_skip) == ("question", "name", texts.Q_NAME, False)
    r = await service.handle_message(msg(A, "  Neon   Fox "))
    assert (r.kind, r.step, r.allow_skip) == ("question", "vrchat", True)
    r = await service.handle_message(msg(A, "foxvr"))
    assert (r.step, r.text, r.allow_skip) == ("logo", texts.Q_LOGO, True)
    r = await service.handle_message(msg(A, "", attachments=1))
    assert r.step == "link" and r.allow_skip is False
    r = await service.handle_message(msg(A, "twitch.tv/neon"))
    assert r.step == "alt_link" and r.allow_skip is True
    r = await service.handle_message(msg(A, "https://youtube.com/@neon"))
    assert r.kind == "summary" and r.step == "summary"
    stored = fake.convs[A]
    assert stored.logo_image is not None
    assert r.image == stored.logo_image
    assert "Neon Fox" in r.text and "https://twitch.tv/neon" in r.text
    assert stored.data["name"] == "Neon Fox"
    assert stored.data["link"] == "https://twitch.tv/neon"
    assert stored.data["alt_link"] == "https://youtube.com/@neon"


async def test_handle_message_without_conversation_returns_none(service):
    assert await service.handle_message(msg(A, "hello")) is None


async def test_start_onboarding_twice_same_question_one_row(service, fake):
    r1 = await service.start_onboarding(A, GUILD)
    saves = fake.saves
    r2 = await service.start_onboarding(A, GUILD)
    assert r1 == r2
    assert fake.saves == saves
    assert list(fake.convs) == [A]


async def test_start_onboarding_continues_at_current_step(service, fake):
    await onboard_to(service, A, "logo")
    r = await service.start_onboarding(A, GUILD)
    assert r.step == "logo"


async def test_start_onboarding_existing_dj_info(service, fake):
    fake.djs[A] = {"dj_name": "X"}
    r = await service.start_onboarding(A, GUILD)
    assert r.kind == "info"
    assert "My DJ profile" in r.text
    assert A not in fake.convs


async def test_db_unavailable_returns_generic_error(ctx, client, fake, sync, roles):
    ctx.db = None
    s = DjConversationService(ctx, client=client)
    r = await s.start_onboarding(A, GUILD)
    assert r == Reply(texts.GENERIC_ERROR, "error")
    assert await s.confirm(A, 1) == Reply(texts.GENERIC_ERROR, "error")
    assert await s.handle_message(msg(A, "x")) == Reply(texts.GENERIC_ERROR, "error")


# --- name step ---


async def test_taken_name_stays_on_name(service, fake):
    fake.taken.add("neon fox")
    await service.start_onboarding(A, GUILD)
    r = await service.handle_message(msg(A, "Neon Fox"))
    assert r.kind == "error" and r.step == "name"
    assert r.text == texts.name_taken("Neon Fox")
    assert fake.convs[A].step == "name"


@pytest.mark.parametrize("variant", ["NEON FOX", "  neon   fox  ", "Neon\tFox"])
async def test_case_and_space_variants_of_taken_name(service, fake, variant):
    fake.taken.add("neon fox")
    await service.start_onboarding(A, GUILD)
    r = await service.handle_message(msg(A, variant))
    assert r.kind == "error" and fake.convs[A].step == "name"


async def test_empty_name(service, fake):
    await service.start_onboarding(A, GUILD)
    r = await service.handle_message(msg(A, "   "))
    assert (r.kind, r.text, r.step) == ("error", texts.NAME_EMPTY, "name")


async def test_overlong_name(service, fake):
    await service.start_onboarding(A, GUILD)
    r = await service.handle_message(msg(A, "x" * 81))
    assert (r.kind, r.text) == ("error", texts.NAME_TOO_LONG)
    assert fake.convs[A].step == "name"


async def test_attachment_only_reply_to_text_question(service, fake, download):
    await service.start_onboarding(A, GUILD)
    r = await service.handle_message(msg(A, "", attachments=1))
    assert (r.kind, r.text) == ("error", texts.NAME_EMPTY)
    download.assert_not_called()
    assert fake.convs[A].step == "name"


async def test_text_step_ignores_attachments(service, fake, download):
    await service.start_onboarding(A, GUILD)
    r = await service.handle_message(msg(A, "Neon Fox", attachments=2))
    assert r.step == "vrchat"
    download.assert_not_called()


async def test_empty_vrchat_is_error_with_skip(service, fake):
    await onboard_to(service, A, "vrchat")
    r = await service.handle_message(msg(A, ""))
    assert r.kind == "error" and r.allow_skip is True and r.step == "vrchat"
    assert "Skip" in r.text


async def test_overlong_vrchat(service, fake):
    await onboard_to(service, A, "vrchat")
    r = await service.handle_message(msg(A, "v" * 81))
    assert r.kind == "error" and fake.convs[A].step == "vrchat"


# --- logo step ---


async def test_two_attachments_logo_many(service, fake, download):
    await onboard_to(service, A, "logo")
    r = await service.handle_message(msg(A, "", attachments=2))
    assert (r.kind, r.text, r.step, r.allow_skip) == ("error", texts.LOGO_MANY, "logo", True)
    assert fake.convs[A].step == "logo"
    download.assert_not_called()


async def test_text_link_logo_needs_file(service, fake):
    await onboard_to(service, A, "logo")
    r = await service.handle_message(msg(A, "https://example.com/logo.png"))
    assert (r.text, r.step) == (texts.LOGO_NEEDS_FILE, "logo")


async def test_bad_file_logo_bad(service, fake, download):
    download.return_value = b"not an image"
    await onboard_to(service, A, "logo")
    r = await service.handle_message(msg(A, "", attachments=1))
    assert (r.kind, r.text, r.step, r.allow_skip) == ("error", texts.LOGO_BAD, "logo", True)
    assert fake.convs[A].logo_image is None


@pytest.mark.parametrize("reason", ["bad_source", "download_failed", "too_big"])
async def test_other_logo_reasons_map_to_bad(service, fake, download, reason):
    download.side_effect = logo_mod.LogoError(reason)
    await onboard_to(service, A, "logo")
    r = await service.handle_message(msg(A, "", attachments=1))
    assert r.text == texts.LOGO_BAD


# --- links ---


async def test_failed_link_check_then_use_as_is(service, fake):
    await onboard_to(service, A, "link")
    r = await service.handle_message(msg(A, "rtmp://my.server/live"))
    assert r.kind == "error" and r.allow_as_is is True and r.step == "link"
    assert r.text == texts.link_check_failed("rtmp://my.server/live")
    assert fake.convs[A].data["pending_link"] == "rtmp://my.server/live"
    await service.record_message_id(A, 10)
    r = await service.use_as_is(A, 10)
    assert r.step == "alt_link"
    data = fake.convs[A].data
    assert data["link"] == "rtmp://my.server/live"
    assert "pending_link" not in data


async def test_use_as_is_normalizes_schemeless(service, fake):
    await onboard_to(service, A, "link")
    await service.handle_message(msg(A, "my.server/live"))
    await service.record_message_id(A, 10)
    await service.use_as_is(A, 10)
    assert fake.convs[A].data["link"] == "https://my.server/live"


async def test_new_message_replaces_pending_link(service, fake):
    await onboard_to(service, A, "link")
    await service.handle_message(msg(A, "example.com/a"))
    r = await service.handle_message(msg(A, "example.com/b"))
    assert r.allow_as_is is True
    assert fake.convs[A].data["pending_link"] == "example.com/b"
    r = await service.handle_message(msg(A, "twitch.tv/ok"))
    assert r.step == "alt_link"
    assert fake.convs[A].data["link"] == "https://twitch.tv/ok"
    assert "pending_link" not in fake.convs[A].data


async def test_use_as_is_without_pending_refused(service, fake):
    await onboard_to(service, A, "link")
    await service.record_message_id(A, 10)
    r = await service.use_as_is(A, 10)
    assert r.kind == "info"
    assert fake.convs[A].step == "link"


async def test_empty_link_error(service, fake):
    await onboard_to(service, A, "link")
    r = await service.handle_message(msg(A, "  "))
    assert r.kind == "error" and fake.convs[A].step == "link"


# --- skip ---


@pytest.mark.parametrize("step", ["name", "link"])
async def test_skip_required_step_refused(service, fake, step):
    await onboard_to(service, A, step)
    await service.record_message_id(A, 10)
    before = copy.deepcopy(fake.convs[A])
    r = await service.skip(A, 10)
    assert r.kind == "info"
    assert fake.convs[A] == before


async def test_skip_optional_step_advances(service, fake):
    await onboard_to(service, A, "vrchat")
    await service.record_message_id(A, 10)
    r = await service.skip(A, 10)
    assert r.step == "logo"
    assert fake.convs[A].data["vrchat"] is None


# --- stale buttons ---


@pytest.mark.parametrize(
    "method", ["skip", "use_as_is", "remove_value", "cancel", "start_over", "confirm"]
)
async def test_stale_message_id_changes_nothing(service, fake, method):
    await onboard_to(service, A, "vrchat")
    await service.record_message_id(A, 10)
    before = copy.deepcopy(fake.convs[A])
    r = await getattr(service, method)(A, 11)
    assert r == Reply(texts.STALE_BUTTON, "info")
    assert fake.convs[A] == before


@pytest.mark.parametrize(
    "method", ["skip", "use_as_is", "remove_value", "cancel", "start_over", "confirm"]
)
async def test_button_without_conversation_is_stale(service, fake, method):
    r = await getattr(service, method)(A, 11)
    assert r == Reply(texts.STALE_BUTTON, "info")
    assert fake.convs == {}


async def test_foreign_user_button_is_stale(service, fake):
    await onboard_to(service, A, "vrchat")
    await service.record_message_id(A, 10)
    r = await service.skip(B, 10)
    assert r == Reply(texts.STALE_BUTTON, "info")
    assert fake.convs[A].step == "vrchat"


# --- cancel / start over ---


async def test_cancel_onboarding_deletes(service, fake):
    await onboard_to(service, A, "vrchat")
    await service.record_message_id(A, 10)
    r = await service.cancel(A, 10)
    assert (r.kind, r.text) == ("cancelled", texts.ONBOARD_CANCELLED)
    assert A not in fake.convs


async def test_start_over_resets(service, fake, download):
    await onboard_to(service, A, "logo")
    await service.handle_message(msg(A, "", attachments=1))
    await service.record_message_id(A, 10)
    r = await service.start_over(A, 10)
    assert (r.step, r.text) == ("name", texts.Q_NAME)
    assert fake.convs[A].data == {}
    assert fake.convs[A].logo_image is None


# --- confirm ---


async def _to_summary(service, user=A):
    await onboard_to(service, user, "logo")
    await service.record_message_id(user, 5)
    await service.skip(user, 5)
    await service.handle_message(msg(user, "twitch.tv/neon"))
    await service.record_message_id(user, 6)
    r = await service.skip(user, 6)
    assert r.kind == "summary"
    await service.record_message_id(user, 7)


async def test_confirm_inserts_assigns_role_logs_once_and_syncs(
    service, fake, ctx, sync, member, role
):
    await _to_summary(service)
    r = await service.confirm(A, 7)
    assert r == Reply(texts.ONBOARD_DONE, "done")
    fake.insert_dj.assert_awaited_once()
    kw = fake.insert_dj.await_args.kwargs
    assert kw["dj_name"] == "Neon Fox"
    assert kw["vrchat_name"] == "foxvr"
    assert kw["stream_url_primary"] == "https://twitch.tv/neon"
    assert kw["stream_url_backup"] is None
    assert kw["logo_image"] is None
    ctx.log_service.log_new_dj.assert_awaited_once_with("Neon Fox", "foxvr")
    member.add_roles.assert_awaited_once()
    assert member.add_roles.await_args.args[0] is role
    sync.assert_awaited_once_with(ctx, A, "alice")
    assert A not in fake.convs


async def test_confirm_with_logo_stores_bytes(service, fake, download):
    await onboard_to(service, A, "logo")
    await service.handle_message(msg(A, "", attachments=1))
    await service.handle_message(msg(A, "twitch.tv/neon"))
    await service.record_message_id(A, 6)
    r = await service.skip(A, 6)
    stored = fake.convs[A].logo_image
    await service.record_message_id(A, 7)
    await service.confirm(A, 7)
    assert r.image == stored
    assert fake.insert_dj.await_args.kwargs["logo_image"] == stored


async def test_confirm_name_became_taken_goes_back(service, fake, ctx, sync):
    await _to_summary(service)
    fake.taken.add("neon fox")
    r = await service.confirm(A, 7)
    assert r.kind == "error" and r.step == "name"
    assert r.text == texts.name_taken("Neon Fox")
    assert fake.convs[A].step == "name"
    fake.insert_dj.assert_not_called()
    ctx.log_service.log_new_dj.assert_not_called()
    sync.assert_not_called()


async def test_confirm_role_failure_is_best_effort(service, fake, member, sync):
    import disnake

    member.add_roles.side_effect = disnake.Forbidden(MagicMock(status=403), "no")
    await _to_summary(service)
    r = await service.confirm(A, 7)
    assert r.kind == "done"
    sync.assert_awaited_once()


async def test_confirm_dj_already_exists_race(service, fake):
    await _to_summary(service)
    fake.djs[A] = {"dj_name": "Other"}
    r = await service.confirm(A, 7)
    assert r.kind == "info"
    fake.insert_dj.assert_not_called()
    assert A not in fake.convs


async def test_confirm_outside_summary_refused(service, fake):
    await onboard_to(service, A, "vrchat")
    await service.record_message_id(A, 10)
    r = await service.confirm(A, 10)
    assert r.kind == "info"
    fake.insert_dj.assert_not_called()


# --- parallel users ---


async def test_two_users_in_parallel_do_not_leak(service, fake):
    fake.taken.add("taken name")
    await service.start_onboarding(A, GUILD)
    await service.start_onboarding(B, GUILD)
    ra, rb = await asyncio.gather(
        service.handle_message(msg(A, "Neon Fox")),
        service.handle_message(msg(B, "Taken Name")),
    )
    assert ra.step == "vrchat" and ra.kind == "question"
    assert rb.step == "name" and rb.kind == "error"
    assert rb.text == texts.name_taken("Taken Name")
    assert fake.convs[A].step == "vrchat"
    assert fake.convs[A].data == {"name": "Neon Fox"}
    assert fake.convs[B].step == "name"
    assert fake.convs[B].data == {}


async def test_same_user_messages_are_serialized(service, fake):
    await service.start_onboarding(A, GUILD)
    r1, r2 = await asyncio.gather(
        service.handle_message(msg(A, "Neon Fox")),
        service.handle_message(msg(A, "foxvr")),
    )
    assert (r1.step, r2.step) == ("vrchat", "logo")
    assert fake.convs[A].data == {"name": "Neon Fox", "vrchat": "foxvr"}


# --- edit ---


def _dj(fake, user=A, **extra):
    fake.djs[user] = {
        "dj_name": "Neon Fox",
        "vrchat_name": "foxvr",
        "logo_url": None,
        "logo_image": None,
        "stream_url_primary": "https://twitch.tv/neon",
        "stream_url_backup": None,
        "active_booking": True,
        **extra,
    }


async def test_start_edit_flags(service, fake):
    _dj(fake)
    r = await service.start_edit(A, GUILD, "vrchat")
    assert (r.kind, r.step, r.allow_skip, r.allow_remove) == ("question", "vrchat", False, True)
    assert r.text == texts.edit_prompt("vrchat")
    r = await service.start_edit(A, GUILD, "link")
    assert (r.allow_remove, r.step) == (False, "link")
    assert fake.convs[A].kind == "edit" and fake.convs[A].step == "link"


async def test_start_edit_replaces_open_onboarding(service, fake):
    await onboard_to(service, A, "vrchat")
    _dj(fake)
    await service.start_edit(A, GUILD, "name")
    assert fake.convs[A].kind == "edit"


async def test_edit_own_name_accepted(service, fake, sync, ctx):
    _dj(fake)
    await service.start_edit(A, GUILD, "name")
    r = await service.handle_message(msg(A, "neon fox"))
    assert fake.dj_name_exists.await_args.kwargs.get("exclude_user_id") == A
    assert r.kind == "overview"
    assert texts.edit_saved(texts.FIELD_LABELS["name"]) in r.text
    assert fake.djs[A]["dj_name"] == "neon fox"
    sync.assert_awaited_once_with(ctx, A, "alice")
    assert A not in fake.convs


async def test_edit_taken_name_stays(service, fake):
    _dj(fake)
    fake.taken.add("other")
    await service.start_edit(A, GUILD, "name")
    r = await service.handle_message(msg(A, "Other"))
    assert r.kind == "error" and fake.convs[A].step == "name"
    fake.update_dj_fields.assert_not_called()


async def test_edit_skip_refused(service, fake):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    await service.record_message_id(A, 3)
    r = await service.skip(A, 3)
    assert r.kind == "info"
    assert fake.convs[A].step == "vrchat"


async def test_edit_remove_value(service, fake, sync):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    await service.record_message_id(A, 3)
    r = await service.remove_value(A, 3)
    assert r.kind == "overview"
    fake.update_dj_fields.assert_awaited_once_with(
        fake.update_dj_fields.await_args.args[0], A, vrchat_name=None
    )
    sync.assert_awaited_once()
    assert A not in fake.convs


async def test_edit_remove_on_required_field_refused(service, fake):
    _dj(fake)
    await service.start_edit(A, GUILD, "link")
    await service.record_message_id(A, 3)
    r = await service.remove_value(A, 3)
    assert r.kind == "info"
    fake.update_dj_fields.assert_not_called()


async def test_edit_logo(service, fake, download, sync):
    _dj(fake)
    await service.start_edit(A, GUILD, "logo")
    r = await service.handle_message(msg(A, "", attachments=1))
    assert r.kind == "overview"
    assert fake.djs[A]["logo_image"] is not None
    assert r.image == fake.djs[A]["logo_image"]
    sync.assert_awaited_once()


async def test_edit_link_use_as_is(service, fake):
    _dj(fake)
    await service.start_edit(A, GUILD, "link")
    r = await service.handle_message(msg(A, "rtspt://my.host/x"))
    assert r.allow_as_is is True and r.allow_remove is False
    await service.record_message_id(A, 4)
    r = await service.use_as_is(A, 4)
    assert r.kind == "overview"
    assert fake.djs[A]["stream_url_primary"] == "rtspt://my.host/x"


async def test_edit_cancel(service, fake):
    _dj(fake)
    await service.start_edit(A, GUILD, "link")
    await service.record_message_id(A, 4)
    r = await service.cancel(A, 4)
    assert r.kind == "cancelled"
    assert A not in fake.convs
    fake.update_dj_fields.assert_not_called()


async def test_start_edit_without_profile(service, fake):
    r = await service.start_edit(A, GUILD, "name")
    assert r == Reply(texts.NO_PROFILE, "info")
    assert A not in fake.convs


# --- overview and group ---


async def test_show_overview(service, fake):
    _dj(fake, logo_image=b"img")
    r = await service.show_overview(A)
    assert r.kind == "overview" and r.image == b"img"
    assert "Active DJ Booking Group: yes" in r.text


async def test_show_overview_no_profile(service, fake):
    assert await service.show_overview(A) == Reply(texts.NO_PROFILE, "info")


async def test_leave_group(service, fake, member, role):
    _dj(fake)
    r = await service.leave_group(A)
    assert fake.djs[A]["active_booking"] is False
    member.remove_roles.assert_awaited_once()
    assert member.remove_roles.await_args.args[0] is role
    assert r.kind == "overview" and "Active DJ Booking Group: no" in r.text


async def test_join_group(service, fake, member, role):
    _dj(fake, active_booking=False)
    r = await service.join_group(A)
    assert fake.djs[A]["active_booking"] is True
    member.add_roles.assert_awaited_once()
    assert "Active DJ Booking Group: yes" in r.text


async def test_leave_group_no_profile(service, fake, member):
    r = await service.leave_group(A)
    assert r == Reply(texts.NO_PROFILE, "info")
    member.remove_roles.assert_not_called()


# --- review fixes ---


async def test_two_djs_confirming_same_name_one_insert(service, fake):
    await _to_summary(service, A)
    await _to_summary(service, B)
    ra, rb = await asyncio.gather(service.confirm(A, 7), service.confirm(B, 7))
    assert fake.insert_dj.await_count == 1
    assert ra.kind == "done"
    assert (rb.kind, rb.step, rb.text) == ("error", "name", texts.name_taken("Neon Fox"))
    assert fake.convs[B].step == "name"
    assert A in fake.djs and B not in fake.djs


async def test_two_djs_editing_to_same_name_one_update(service, fake):
    _dj(fake, A, dj_name="Alpha")
    _dj(fake, B, dj_name="Beta")
    await service.start_edit(A, GUILD, "name")
    await service.start_edit(B, GUILD, "name")
    ra, rb = await asyncio.gather(
        service.handle_message(msg(A, "Gamma")), service.handle_message(msg(B, "gamma"))
    )
    assert fake.update_dj_fields.await_count == 1
    assert ra.kind == "overview"
    assert (rb.kind, rb.step) == ("error", "name")
    assert fake.djs[B]["dj_name"] == "Beta"


async def test_edit_name_vs_onboarding_confirm_same_name(service, fake):
    _dj(fake, B, dj_name="Beta")
    await _to_summary(service, A)
    await service.start_edit(B, GUILD, "name")
    ra, rb = await asyncio.gather(
        service.confirm(A, 7), service.handle_message(msg(B, "Neon Fox"))
    )
    written = fake.insert_dj.await_count + fake.update_dj_fields.await_count
    assert written == 1
    assert sorted([ra.kind, rb.kind]) in (["done", "error"], ["error", "overview"])


async def test_delete_failure_after_insert_still_done(
    service, fake, ctx, sync, member, monkeypatch
):
    await _to_summary(service)
    monkeypatch.setattr(
        svc_mod, "delete_conversation", AsyncMock(side_effect=RuntimeError("db gone"))
    )
    r = await service.confirm(A, 7)
    assert r == Reply(texts.ONBOARD_DONE, "done")
    fake.insert_dj.assert_awaited_once()
    ctx.log_service.log_new_dj.assert_awaited_once_with("Neon Fox", "foxvr")
    member.add_roles.assert_awaited_once()
    sync.assert_awaited_once()
    # The leftover summary: pressing Confirm again does not log a second time.
    assert A in fake.convs
    monkeypatch.setattr(svc_mod, "delete_conversation", fake.delete_conversation)
    r = await service.confirm(A, 7)
    assert r == Reply(texts.ALREADY_DJ, "info")
    ctx.log_service.log_new_dj.assert_awaited_once()
    fake.insert_dj.assert_awaited_once()
    assert A not in fake.convs


async def test_post_insert_steps_run_before_delete(service, fake, ctx, sync, monkeypatch):
    order: list[str] = []
    ctx.log_service.log_new_dj.side_effect = lambda *a: order.append("log")
    sync.side_effect = lambda *a: order.append("sync")

    async def delete(pool, user_id):
        order.append("delete")
        await fake.delete_conversation(pool, user_id)

    await _to_summary(service)
    monkeypatch.setattr(svc_mod, "delete_conversation", delete)
    await service.confirm(A, 7)
    assert order == ["log", "sync", "delete"]


async def test_log_failure_after_insert_still_done(service, fake, ctx, sync):
    ctx.log_service.log_new_dj.side_effect = RuntimeError("discord down")
    await _to_summary(service)
    r = await service.confirm(A, 7)
    assert r.kind == "done"
    sync.assert_awaited_once()
    assert A not in fake.convs


async def test_link_500_chars_ok(service, fake):
    await onboard_to(service, A, "link")
    link = "https://twitch.tv/" + "x" * (500 - len("https://twitch.tv/"))
    assert len(link) == 500
    r = await service.handle_message(msg(A, link))
    assert r.step == "alt_link"
    assert fake.convs[A].data["link"] == link


async def test_link_501_chars_rejected(service, fake):
    await onboard_to(service, A, "link")
    link = "https://twitch.tv/" + "x" * (501 - len("https://twitch.tv/"))
    r = await service.handle_message(msg(A, link))
    assert (r.kind, r.text, r.step) == ("error", texts.LINK_TOO_LONG, "link")
    assert "pending_link" not in fake.convs[A].data
    assert "link" not in fake.convs[A].data


async def test_long_failed_link_is_echoed_truncated(service, fake):
    await onboard_to(service, A, "link")
    link = "example.com/" + "y" * 400
    r = await service.handle_message(msg(A, link))
    assert r.allow_as_is is True
    assert link not in r.text
    assert link[:200] + "..." in r.text
    assert fake.convs[A].data["pending_link"] == link


def test_link_check_failed_truncates():
    assert "a" * 200 in texts.link_check_failed("a" * 200)
    assert "a" * 200 + "..." not in texts.link_check_failed("a" * 200)
    long_text = texts.link_check_failed("b" * 300)
    assert "b" * 200 + "..." in long_text
    assert "b" * 201 not in long_text


async def test_record_message_id_failure_is_logged(service, fake, monkeypatch, caplog):
    await service.start_onboarding(A, GUILD)
    monkeypatch.setattr(
        svc_mod, "set_conversation_message", AsyncMock(side_effect=RuntimeError("down"))
    )
    with caplog.at_level("WARNING", logger="bot.services.dj_conversation"):
        assert await service.record_message_id(A, 9) is None
    records = [r for r in caplog.records if r.name == "bot.services.dj_conversation"]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is not None


# --- not now ---


async def test_decline_deletes_open_onboarding_conversation(service, fake):
    await onboard_to(service, A, "vrchat")
    r = await service.decline(A)
    assert r == Reply(texts.ONBOARD_DECLINED, "info")
    assert A not in fake.convs


async def test_decline_keeps_an_open_edit_conversation(service, fake):
    fake.djs[A] = {"dj_name": "Neon Fox", "active_booking": True}
    await service.start_edit(A, GUILD, "vrchat")
    r = await service.decline(A)
    assert r == Reply(texts.ONBOARD_DECLINED, "info")
    assert fake.convs[A].kind == "edit"


async def test_decline_without_conversation_only_answers(service, fake):
    assert await service.decline(A) == Reply(texts.ONBOARD_DECLINED, "info")
    assert fake.convs == {}


def test_stale_text_points_to_the_latest_message():
    assert texts.STALE_BUTTON == (
        "This message is out of date. Use the latest message from me, "
        "or press Cancel there."
    )


# --- edit expiry ---

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _conv(kind: str, updated_at: datetime | None) -> Conversation:
    return Conversation(A, GUILD, kind, "vrchat", updated_at=updated_at)


def test_edit_expiry_is_one_hour():
    assert EDIT_EXPIRY == timedelta(hours=1)


@pytest.mark.parametrize(
    ("kind", "age", "expected"),
    [
        ("edit", None, False),
        ("edit", timedelta(minutes=59, seconds=59), False),
        ("edit", timedelta(hours=1), False),
        ("edit", timedelta(hours=1, seconds=1), True),
        ("onboarding", timedelta(days=3), False),
    ],
)
def test_is_expired(kind, age, expected):
    updated = None if age is None else NOW - age
    assert is_expired(_conv(kind, updated), now=NOW) is expected


def test_is_expired_treats_naive_timestamp_as_utc():
    naive = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    assert is_expired(_conv("edit", naive), now=NOW) is True
    naive = (NOW - timedelta(minutes=30)).replace(tzinfo=None)
    assert is_expired(_conv("edit", naive), now=NOW) is False


def test_is_expired_default_now_is_current_time():
    old = datetime.now(UTC) - timedelta(hours=2)
    assert is_expired(_conv("edit", old)) is True
    assert is_expired(_conv("edit", datetime.now(UTC))) is False


def _age(fake, user, age):
    fake.convs[user].updated_at = datetime.now(UTC) - age


async def test_expired_edit_message_falls_through_and_deletes_row(service, fake, sync):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    _age(fake, A, timedelta(hours=1, minutes=1))
    assert await service.handle_message(msg(A, "newvr")) is None
    assert A not in fake.convs
    assert fake.djs[A]["vrchat_name"] == "foxvr"
    fake.update_dj_fields.assert_not_called()


async def test_edit_just_under_one_hour_still_works(service, fake, sync):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    _age(fake, A, timedelta(minutes=59))
    r = await service.handle_message(msg(A, "newvr"))
    assert r is not None and r.kind == "overview"
    assert fake.djs[A]["vrchat_name"] == "newvr"


async def test_old_onboarding_conversation_still_works(service, fake):
    await service.start_onboarding(A, GUILD)
    _age(fake, A, timedelta(days=2))
    r = await service.handle_message(msg(A, "Neon Fox"))
    assert r is not None and (r.kind, r.step) == ("question", "vrchat")


async def test_expired_edit_delete_failure_still_falls_through(service, fake, monkeypatch):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    _age(fake, A, timedelta(hours=2))
    monkeypatch.setattr(svc_mod, "delete_conversation", AsyncMock(side_effect=RuntimeError("x")))
    assert await service.handle_message(msg(A, "newvr")) is None
    assert fake.djs[A]["vrchat_name"] == "foxvr"


@pytest.mark.parametrize(
    "method", ["skip", "use_as_is", "remove_value", "cancel", "start_over", "confirm"]
)
async def test_button_on_expired_edit_is_stale_and_deletes(service, fake, method):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    await service.record_message_id(A, 10)
    _age(fake, A, timedelta(hours=2))
    r = await getattr(service, method)(A, 10)
    assert r == Reply(texts.STALE_BUTTON, "info")
    assert A not in fake.convs
    assert fake.djs[A]["vrchat_name"] == "foxvr"


async def test_start_onboarding_ignores_expired_edit(service, fake):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    _age(fake, A, timedelta(hours=2))
    r = await service.start_onboarding(A, GUILD)
    assert r == Reply(texts.ALREADY_DJ, "info")
    assert A not in fake.convs


async def test_start_edit_replaces_expired_edit(service, fake):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    _age(fake, A, timedelta(hours=2))
    r = await service.start_edit(A, GUILD, "link")
    assert r.step == "link" and fake.convs[A].step == "link"


async def test_has_open_conversation(service, fake):
    assert await service.has_open_conversation(A) is False
    await service.start_onboarding(A, GUILD)
    _age(fake, A, timedelta(days=2))
    assert await service.has_open_conversation(A) is True
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    assert await service.has_open_conversation(A) is True
    _age(fake, A, timedelta(hours=2))
    assert await service.has_open_conversation(A) is False
    assert A not in fake.convs


async def test_has_open_conversation_delete_failure_is_only_logged(service, fake, monkeypatch):
    _dj(fake)
    await service.start_edit(A, GUILD, "vrchat")
    _age(fake, A, timedelta(hours=2))
    monkeypatch.setattr(svc_mod, "get_conversation", AsyncMock(
        side_effect=[copy.deepcopy(fake.convs[A]), RuntimeError("db down")]
    ))
    assert await service.has_open_conversation(A) is False


async def test_has_open_conversation_without_db(ctx, client, fake, sync, roles):
    ctx.db = None
    assert await DjConversationService(ctx, client=client).has_open_conversation(A) is False
