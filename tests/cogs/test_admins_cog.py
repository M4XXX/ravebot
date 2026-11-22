"""Tests for bot/cogs/admins_cog.py (/admins add, remove, list; owners only)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from disnake.ext import commands

from bot.services.admin_management import AdminChange
from bot.services.admin_registry import AdminRegistry
from bot.ui import texts
from bot.ui.gates import ADMIN_IDLE_REPLY

OWNER = 1
ADMIN = 2
STRANGER = 99
DENIED = "You don't have permission to use this command."
OWNER_ONLY = "Only the bot owners can manage the admin list."


def _ctx(active: bool = True) -> MagicMock:
    ctx = MagicMock()
    registry = AdminRegistry({OWNER, 3, 4}, MagicMock())
    registry._added = {ADMIN}
    registry._loaded = True
    registry.ensure_loaded = AsyncMock()
    ctx.admins = registry
    ctx.is_admin = registry.is_admin
    ctx.db = MagicMock()
    ctx._active = active
    return ctx


def _service() -> MagicMock:
    service = MagicMock()
    service.add = AsyncMock(return_value=AdminChange(True, "Added."))
    service.remove = AsyncMock(return_value=AdminChange(True, "Removed."))
    service.list_admins = AsyncMock(return_value=[(OWNER, "Boss", True)])
    return service


def _inter(uid: int = OWNER, guild_id: int | None = 111):
    inter = MagicMock()
    inter.author.id = uid
    inter.author.display_name = "Actor"
    inter.guild_id = guild_id
    inter.response = MagicMock()
    done = {"v": False}

    async def _send(*a, **k):
        done["v"] = True

    inter.response.send_message = AsyncMock(side_effect=_send)
    inter.response.defer = AsyncMock(side_effect=_send)
    inter.response.is_done = lambda: done["v"]
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.fixture(autouse=True)
def _active(monkeypatch):
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=True))


def _cog(ctx=None, service=None):
    from bot.cogs.admins_cog import AdminsCog

    return AdminsCog(ctx or _ctx(), service or _service())


async def _drive(cog, inter, call):
    """Run the way disnake does: the check, the error handler on failure, else the callback."""
    try:
        ok = await cog.cog_slash_command_check(inter)
    except commands.CheckFailure as error:
        await cog.cog_slash_command_error(inter, error)
        return False
    if not ok:
        await cog.cog_slash_command_error(inter, commands.CheckFailure("check"))
        return False
    await call()
    return True


def _calls(cog, inter):
    user = MagicMock()
    user.id = 50
    return {
        "add": lambda: cog.admins_add.callback(cog, inter, user),
        "remove": lambda: cog.admins_remove.callback(cog, inter, user),
        "list": lambda: cog.admins_list.callback(cog, inter),
    }


def _sent(inter) -> list[str]:
    return [c.args[0] for c in inter.response.send_message.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["add", "remove", "list"])
async def test_non_admin_gets_generic_denial(sub) -> None:
    service = _service()
    cog = _cog(service=service)
    inter = _inter(STRANGER)
    ran = await _drive(cog, inter, _calls(cog, inter)[sub])
    assert ran is False
    assert _sent(inter) == [DENIED]
    assert inter.response.send_message.call_args.kwargs["ephemeral"] is True
    service.add.assert_not_awaited()
    service.remove.assert_not_awaited()
    service.list_admins.assert_not_awaited()
    inter.response.defer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["add", "remove", "list"])
async def test_admin_who_is_not_owner_gets_owner_text(sub) -> None:
    ctx = _ctx()
    service = _service()
    cog = _cog(ctx, service)
    inter = _inter(ADMIN)
    before = (set(ctx.admins.all_ids()), ctx.admins._added.copy())
    ran = await _drive(cog, inter, _calls(cog, inter)[sub])
    assert ran is False
    assert _sent(inter) == [OWNER_ONLY]
    assert inter.response.send_message.call_args.kwargs["ephemeral"] is True
    service.add.assert_not_awaited()
    service.remove.assert_not_awaited()
    service.list_admins.assert_not_awaited()
    assert (set(ctx.admins.all_ids()), ctx.admins._added) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["add", "remove", "list"])
async def test_missing_registry_fails_closed(sub) -> None:
    ctx = _ctx()
    ctx.admins = None
    ctx.is_admin = lambda uid: True
    cog = _cog(ctx)
    inter = _inter(OWNER)
    ran = await _drive(cog, inter, _calls(cog, inter)[sub])
    assert ran is False
    assert _sent(inter) == [OWNER_ONLY]


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["add", "remove", "list"])
async def test_dm_gets_server_only_answer(sub) -> None:
    service = _service()
    cog = _cog(service=service)
    inter = _inter(OWNER, guild_id=None)
    ran = await _drive(cog, inter, _calls(cog, inter)[sub])
    assert ran is False
    assert _sent(inter) == [texts.ADMIN_SERVER_ONLY]
    service.list_admins.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["add", "remove", "list"])
async def test_idle_gets_idle_reply(sub, monkeypatch) -> None:
    monkeypatch.setattr("bot.services.activation.is_active", AsyncMock(return_value=False))
    service = _service()
    cog = _cog(service=service)
    inter = _inter(OWNER)
    ran = await _drive(cog, inter, _calls(cog, inter)[sub])
    assert ran is False
    assert _sent(inter) == [ADMIN_IDLE_REPLY]
    service.add.assert_not_awaited()
    service.remove.assert_not_awaited()
    service.list_admins.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_passes_actor_and_result_text() -> None:
    service = _service()
    service.add.return_value = AdminChange(True, "Added Someone.")
    cog = _cog(service=service)
    inter = _inter(OWNER)
    assert await _drive(cog, inter, _calls(cog, inter)["add"]) is True
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    service.add.assert_awaited_once_with(50, actor="Actor")
    inter.followup.send.assert_awaited_once_with("Added Someone.", ephemeral=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Owners can only be changed with ADMIN_USER_IDS.",
                                  "This person is not an added admin."])
async def test_remove_passes_refusal_text(text) -> None:
    service = _service()
    service.remove.return_value = AdminChange(False, text)
    cog = _cog(service=service)
    inter = _inter(OWNER)
    assert await _drive(cog, inter, _calls(cog, inter)["remove"]) is True
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    service.remove.assert_awaited_once_with(50, actor="Actor")
    inter.followup.send.assert_awaited_once_with(text, ephemeral=True)


@pytest.mark.asyncio
async def test_list_splits_messages_and_escapes_names() -> None:
    service = _service()
    rows = [(i, "Owner Name", True) for i in (1, 3, 4)]
    rows += [(1000 + i, "@everyone **x**" if i == 0 else None, False) for i in range(120)]
    service.list_admins.return_value = rows
    cog = _cog(service=service)
    inter = _inter(OWNER)
    assert await _drive(cog, inter, _calls(cog, inter)["list"]) is True
    inter.response.defer.assert_awaited_once_with(ephemeral=True)
    sent = [c for c in inter.followup.send.call_args_list]
    assert all(c.kwargs["ephemeral"] is True for c in sent)
    messages = [c.args[0] for c in sent]
    assert len(messages) > 1
    assert all(len(m) <= 1900 for m in messages)
    lines = "\n".join(messages).split("\n")
    assert len(lines) == 123
    assert lines[0] == "Owner: Owner Name (ID 1)"
    assert lines[3].startswith("Admin: ")
    assert "Admin: unknown user (ID 1001)" in lines
    for uid in [1, 3, 4] + [1000 + i for i in range(120)]:
        assert sum(f"(ID {uid})" in line for line in lines) == 1
    crafted = next(line for line in lines if "(ID 1000)" in line)
    assert "@everyone" not in crafted.replace("@​everyone", "")
    assert "**x**" not in crafted


@pytest.mark.asyncio
async def test_list_with_only_owners_sends_one_message() -> None:
    service = _service()
    service.list_admins.return_value = [(1, "Boss", True)]
    cog = _cog(service=service)
    inter = _inter(OWNER)
    await _drive(cog, inter, _calls(cog, inter)["list"])
    inter.followup.send.assert_awaited_once_with("Owner: Boss (ID 1)", ephemeral=True)


@pytest.mark.asyncio
async def test_unexpected_error_answers_generic_text() -> None:
    cog = _cog()
    inter = _inter(OWNER)
    await cog.cog_slash_command_error(inter, RuntimeError("boom"))
    assert _sent(inter) == ["Something went wrong. Please try again in a moment."]
    inter2 = _inter(OWNER)
    await inter2.response.defer()
    await cog.cog_slash_command_error(inter2, RuntimeError("boom"))
    inter2.followup.send.assert_awaited_once_with(
        "Something went wrong. Please try again in a moment.", ephemeral=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("sub", "word"), [("add", "Added"), ("remove", "Removed")])
async def test_success_answer_escapes_the_name(sub, word) -> None:
    service = _service()
    change = AdminChange(True, f"{word} @everyone **x**.", name="@everyone **x**")
    getattr(service, sub).return_value = change
    cog = _cog(service=service)
    inter = _inter(OWNER)
    assert await _drive(cog, inter, _calls(cog, inter)[sub]) is True
    text = inter.followup.send.call_args.args[0]
    assert text.startswith(f"{word} ") and text.endswith(".")
    assert "@everyone" not in text
    assert "\\*\\*x\\*\\*" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["add", "remove"])
async def test_actor_is_cleaned(sub) -> None:
    service = _service()
    cog = _cog(service=service)
    inter = _inter(OWNER)
    inter.author.display_name = "Max‮evil"
    await _drive(cog, inter, _calls(cog, inter)[sub])
    getattr(service, sub).assert_awaited_once_with(50, actor="Maxevil")


@pytest.mark.asyncio
async def test_actor_falls_back_to_id() -> None:
    service = _service()
    cog = _cog(service=service)
    inter = _inter(OWNER)
    inter.author.display_name = "‮"
    await _drive(cog, inter, _calls(cog, inter)["add"])
    service.add.assert_awaited_once_with(50, actor=str(OWNER))
