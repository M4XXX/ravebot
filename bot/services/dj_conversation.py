"""Guided DM conversation for DJ onboarding and profile edits.

All state lives in the dj_conversations row of the user, so a restart loses
nothing and two DJs never share state. Every public method runs under a
per-user asyncio.Lock owned by this service (separate from the booking lock).
The service never sends Discord messages: it returns a Reply that the UI
turns into a message with the matching buttons.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import asyncpg
import disnake

from bot.db.dj_conversations import (
    Conversation,
    delete_conversation,
    get_conversation,
    save_conversation,
    set_conversation_message,
)
from bot.db.djs import (
    dj_name_exists,
    get_dj_by_discord_id,
    insert_dj,
    update_active_booking,
    update_dj_fields,
)
from bot.services.dj_sync import sync_dj
from bot.services.dj_validation import (
    MAX_LINK_LENGTH,
    MAX_VRCHAT_LENGTH,
    clean_text,
    name_error,
    normalize_link,
    stream_link_ok,
)
from bot.services.logo import LogoError, logo_from_message
from bot.services.roles import get_invites_role
from bot.ui import texts

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)

STEPS = ("name", "vrchat", "logo", "link", "alt_link")
SUMMARY = "summary"
OPTIONAL_STEPS = frozenset({"vrchat", "logo", "alt_link"})
LINK_STEPS = frozenset({"link", "alt_link"})
ONBOARDING = "onboarding"
EDIT = "edit"

_QUESTIONS = {
    "name": texts.Q_NAME,
    "vrchat": texts.Q_VRCHAT,
    "logo": texts.Q_LOGO,
    "link": texts.Q_LINK,
    "alt_link": texts.Q_ALT_LINK,
}
_COLUMNS = {
    "name": "dj_name",
    "vrchat": "vrchat_name",
    "logo": "logo_image",
    "link": "stream_url_primary",
    "alt_link": "stream_url_backup",
}
_LOGO_TEXTS = {"many": texts.LOGO_MANY, "needs_file": texts.LOGO_NEEDS_FILE}

ReplyKind = Literal["question", "summary", "error", "done", "info", "cancelled", "overview"]


@dataclass
class Reply:
    text: str
    kind: ReplyKind
    step: str | None = None
    image: bytes | None = None
    allow_skip: bool = False
    allow_as_is: bool = False
    allow_remove: bool = False
    in_group: bool = False  # overview only: the DJ is in the Active DJ Booking Group


def _stale() -> Reply:
    return Reply(texts.STALE_BUTTON, "info")


def _generic() -> Reply:
    return Reply(texts.GENERIC_ERROR, "error")


def _prompt(conv: Conversation, text: str, kind: ReplyKind, allow_as_is: bool = False) -> Reply:
    optional = conv.step in OPTIONAL_STEPS
    return Reply(
        text,
        kind,
        step=conv.step,
        allow_skip=conv.kind == ONBOARDING and optional,
        allow_as_is=allow_as_is,
        allow_remove=conv.kind == EDIT and optional,
    )


def _question(conv: Conversation) -> Reply:
    if conv.kind == EDIT:
        return _prompt(conv, texts.edit_prompt(conv.step), "question")
    return _prompt(conv, _QUESTIONS[conv.step], "question")


def _error(conv: Conversation, text: str, allow_as_is: bool = False) -> Reply:
    return _prompt(conv, text, "error", allow_as_is)


def _summary(conv: Conversation) -> Reply:
    data = conv.data
    body = texts.profile_text(
        dj_name=data.get("name") or "",
        vrchat_name=data.get("vrchat"),
        logo=None,
        logo_attached=conv.logo_image is not None,
        stream_link=data.get("link"),
        alt_link=data.get("alt_link"),
    )
    return Reply(
        f"{texts.SUMMARY_TITLE}\n{body}", "summary", step=SUMMARY, image=conv.logo_image
    )


def _current(conv: Conversation) -> Reply:
    if conv.step == SUMMARY:
        return _summary(conv)
    pending = conv.data.get("pending_link")
    if pending is not None and conv.step in LINK_STEPS:
        return _error(conv, texts.link_check_failed(pending), allow_as_is=True)
    return _question(conv)


class DjConversationService:
    def __init__(self, ctx: "BotContext", client: disnake.Client | None = None) -> None:
        self._ctx = ctx
        self._client = client
        self._locks: dict[int, asyncio.Lock] = {}
        # Service-wide: covers a DJ-name check through the write of that name, so two
        # DJs cannot save the same name. Always taken after the per-user lock.
        self._name_lock = asyncio.Lock()

    def _lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = self._locks[user_id] = asyncio.Lock()
        return lock

    async def _guarded(
        self, user_id: int, fn: Callable[[asyncpg.Pool], Awaitable[Reply | None]]
    ) -> Reply | None:
        db = self._ctx.db
        if db is None:
            return _generic()
        async with self._lock(user_id):
            try:
                return await fn(db)
            except Exception:
                logger.exception("DJ conversation failed for user %s", user_id)
                return _generic()

    async def _run(
        self, user_id: int, fn: Callable[[asyncpg.Pool], Awaitable[Reply]]
    ) -> Reply:
        reply = await self._guarded(user_id, fn)
        assert reply is not None
        return reply

    async def _button(
        self,
        user_id: int,
        message_id: int,
        fn: Callable[[asyncpg.Pool, Conversation], Awaitable[Reply]],
    ) -> Reply:
        async def body(db: asyncpg.Pool) -> Reply:
            conv = await get_conversation(db, user_id)
            if conv is None or conv.message_id is None or conv.message_id != message_id:
                return _stale()
            return await fn(db, conv)

        return await self._run(user_id, body)

    # --- starting ---

    async def start_onboarding(self, user_id: int, guild_id: int) -> Reply:
        async def body(db: asyncpg.Pool) -> Reply:
            conv = await get_conversation(db, user_id)
            if conv is not None:
                return _current(conv)
            if await get_dj_by_discord_id(db, user_id) is not None:
                return Reply(texts.ALREADY_DJ, "info")
            conv = Conversation(user_id, guild_id, ONBOARDING, STEPS[0])
            await save_conversation(db, conv)
            return _question(conv)

        return await self._run(user_id, body)

    async def start_edit(self, user_id: int, guild_id: int, field: str) -> Reply:
        if field not in STEPS:
            raise ValueError(f"Unknown DJ profile field: {field}")

        async def body(db: asyncpg.Pool) -> Reply:
            if await get_dj_by_discord_id(db, user_id) is None:
                return Reply(texts.NO_PROFILE, "info")
            conv = Conversation(user_id, guild_id, EDIT, field)
            await save_conversation(db, conv)
            return _question(conv)

        return await self._run(user_id, body)

    async def record_message_id(self, user_id: int, message_id: int) -> None:
        async def body(db: asyncpg.Pool) -> None:
            try:
                await set_conversation_message(db, user_id, message_id)
            except Exception:
                logger.warning(
                    "Recording the conversation message of %s failed", user_id, exc_info=True
                )

        if self._ctx.db is None:
            return
        await self._guarded(user_id, body)

    # --- answers ---

    async def handle_message(self, message: disnake.Message) -> Reply | None:
        user_id = message.author.id

        async def body(db: asyncpg.Pool) -> Reply | None:
            conv = await get_conversation(db, user_id)
            if conv is None:
                return None
            return await self._answer(db, conv, message)

        return await self._guarded(user_id, body)

    async def _answer(
        self, db: asyncpg.Pool, conv: Conversation, message: disnake.Message
    ) -> Reply:
        step = conv.step
        if step == SUMMARY:
            return _summary(conv)
        if step == "logo":
            try:
                image = await logo_from_message(message)
            except LogoError as exc:
                return _error(conv, _LOGO_TEXTS.get(exc.reason, texts.LOGO_BAD))
            except Exception:
                logger.warning("Logo processing failed for %s", conv.discord_user_id, exc_info=True)
                return _error(conv, texts.LOGO_BAD)
            return await self._accept(db, conv, image)

        text = clean_text(message.content or "")
        if step == "name":
            problem = name_error(text)
            if problem == "empty":
                return _error(conv, texts.NAME_EMPTY)
            if problem == "too_long":
                return _error(conv, texts.NAME_TOO_LONG)
            if conv.kind == EDIT:
                # The check and the write happen under the name lock in _finish_edit.
                return await self._finish_edit(db, conv, text)
            if await dj_name_exists(db, text):
                return _error(conv, texts.name_taken(text))
            return await self._accept(db, conv, text)
        if step == "vrchat":
            if not text:
                empty = texts.VRCHAT_EMPTY_OPTIONAL if conv.kind == ONBOARDING else texts.NAME_EMPTY
                return _error(conv, empty)
            if len(text) > MAX_VRCHAT_LENGTH:
                return _error(conv, texts.NAME_TOO_LONG)
            return await self._accept(db, conv, text)
        # link steps
        if not text:
            optional = conv.kind == ONBOARDING and step in OPTIONAL_STEPS
            return _error(conv, texts.LINK_EMPTY_OPTIONAL if optional else texts.LINK_EMPTY)
        if len(text) > MAX_LINK_LENGTH:
            return _error(conv, texts.LINK_TOO_LONG)
        if not stream_link_ok(text):
            conv.data["pending_link"] = text
            await save_conversation(db, conv)
            return _error(conv, texts.link_check_failed(text), allow_as_is=True)
        return await self._accept(db, conv, normalize_link(text))

    async def _accept(self, db: asyncpg.Pool, conv: Conversation, value: Any) -> Reply:
        """Store a valid value for the current step and move on (or finish an edit)."""
        conv.data.pop("pending_link", None)
        if conv.kind == EDIT:
            return await self._finish_edit(db, conv, value)
        if conv.step == "logo":
            conv.logo_image = value
        else:
            conv.data[conv.step] = value
        index = STEPS.index(conv.step)
        conv.step = STEPS[index + 1] if index + 1 < len(STEPS) else SUMMARY
        conv.message_id = None
        await save_conversation(db, conv)
        return _current(conv)

    # --- buttons ---

    async def skip(self, user_id: int, message_id: int) -> Reply:
        async def fn(db: asyncpg.Pool, conv: Conversation) -> Reply:
            if conv.kind != ONBOARDING or conv.step not in OPTIONAL_STEPS:
                return _stale()
            return await self._accept(db, conv, None)

        return await self._button(user_id, message_id, fn)

    async def use_as_is(self, user_id: int, message_id: int) -> Reply:
        async def fn(db: asyncpg.Pool, conv: Conversation) -> Reply:
            pending = conv.data.get("pending_link")
            if pending is None or conv.step not in LINK_STEPS:
                return _stale()
            return await self._accept(db, conv, normalize_link(pending))

        return await self._button(user_id, message_id, fn)

    async def remove_value(self, user_id: int, message_id: int) -> Reply:
        async def fn(db: asyncpg.Pool, conv: Conversation) -> Reply:
            if conv.kind != EDIT or conv.step not in OPTIONAL_STEPS:
                return _stale()
            conv.data.pop("pending_link", None)
            return await self._finish_edit(db, conv, None)

        return await self._button(user_id, message_id, fn)

    async def cancel(self, user_id: int, message_id: int) -> Reply:
        async def fn(db: asyncpg.Pool, conv: Conversation) -> Reply:
            await delete_conversation(db, user_id)
            if conv.kind == EDIT:
                return Reply(texts.EDIT_CANCELLED, "cancelled")
            return Reply(texts.ONBOARD_CANCELLED, "cancelled")

        return await self._button(user_id, message_id, fn)

    async def start_over(self, user_id: int, message_id: int) -> Reply:
        async def fn(db: asyncpg.Pool, conv: Conversation) -> Reply:
            if conv.kind != ONBOARDING:
                return _stale()
            conv.data = {}
            conv.logo_image = None
            conv.step = STEPS[0]
            conv.message_id = None
            await save_conversation(db, conv)
            return _question(conv)

        return await self._button(user_id, message_id, fn)

    async def confirm(self, user_id: int, message_id: int) -> Reply:
        async def fn(db: asyncpg.Pool, conv: Conversation) -> Reply:
            if conv.kind != ONBOARDING or conv.step != SUMMARY:
                return _stale()
            return await self._finish_onboarding(db, conv)

        return await self._button(user_id, message_id, fn)

    # --- finishing ---

    async def _back_to(self, db: asyncpg.Pool, conv: Conversation, step: str) -> None:
        conv.step = step
        conv.data.pop(step, None)
        conv.message_id = None
        await save_conversation(db, conv)

    async def _finish_onboarding(self, db: asyncpg.Pool, conv: Conversation) -> Reply:
        user_id = conv.discord_user_id
        data = conv.data
        name = data.get("name")
        link = data.get("link")
        if not name or not link:
            await self._back_to(db, conv, "name" if not name else "link")
            return _question(conv)
        if await get_dj_by_discord_id(db, user_id) is not None:
            # A row and a leftover summary: the post-insert steps cannot be told apart
            # from done ones without double logging, so only clear the conversation.
            await self._drop(db, user_id)
            return Reply(texts.ALREADY_DJ, "info")
        vrchat = data.get("vrchat") or None
        async with self._name_lock:
            if await dj_name_exists(db, name):
                await self._back_to(db, conv, "name")
                return _error(conv, texts.name_taken(name))
            try:
                await insert_dj(
                    db,
                    discord_user_id=user_id,
                    dj_name=name,
                    vrchat_name=vrchat,
                    logo_url=None,
                    stream_url_primary=link,
                    stream_url_backup=data.get("alt_link") or None,
                    logo_image=conv.logo_image,
                )
            except asyncpg.UniqueViolationError:
                await self._drop(db, user_id)
                return Reply(texts.ALREADY_DJ, "info")
        # Saved: every remaining step is best effort and must not undo the success.
        log_service = self._ctx.log_service
        if log_service is not None:
            try:
                await log_service.log_new_dj(name, vrchat)
            except Exception:
                logger.warning("Logging the new DJ %s failed", user_id, exc_info=True)
        await self._set_invites_role(db, user_id, add=True, guild_id=conv.guild_id)
        await sync_dj(self._ctx, user_id, self._discord_name(user_id, conv.guild_id, name))
        await self._drop(db, user_id)
        return Reply(texts.ONBOARD_DONE, "done")

    async def _drop(self, db: asyncpg.Pool, user_id: int) -> None:
        """Delete the conversation after a finished save; a failure is only logged."""
        try:
            await delete_conversation(db, user_id)
        except Exception:
            logger.warning("Deleting the conversation of %s failed", user_id, exc_info=True)

    async def _finish_edit(self, db: asyncpg.Pool, conv: Conversation, value: Any) -> Reply:
        user_id = conv.discord_user_id
        field = conv.step
        if field == "name":
            async with self._name_lock:
                if await dj_name_exists(db, value, exclude_user_id=user_id):
                    return _error(conv, texts.name_taken(value))
                updated = await update_dj_fields(db, user_id, dj_name=value)
        else:
            updated = await update_dj_fields(db, user_id, **{_COLUMNS[field]: value})
        if not updated:
            await delete_conversation(db, user_id)
            return Reply(texts.NO_PROFILE, "info")
        row = await get_dj_by_discord_id(db, user_id)
        fallback = row["dj_name"] if row is not None else ""
        await sync_dj(self._ctx, user_id, self._discord_name(user_id, conv.guild_id, fallback))
        await self._drop(db, user_id)
        overview = await self._overview(db, user_id)
        if overview.kind != "overview":
            return overview
        overview.text = f"{texts.edit_saved(texts.FIELD_LABELS[field])}\n\n{overview.text}"
        return overview

    # --- overview and booking group ---

    async def _overview(self, db: asyncpg.Pool, user_id: int) -> Reply:
        row = await get_dj_by_discord_id(db, user_id)
        if row is None:
            return Reply(texts.NO_PROFILE, "info")
        image = row["logo_image"]
        body = texts.profile_text(
            dj_name=row["dj_name"],
            vrchat_name=row["vrchat_name"],
            logo=row["logo_url"],
            logo_attached=image is not None,
            stream_link=row["stream_url_primary"],
            alt_link=row["stream_url_backup"],
            show_group=bool(row["active_booking"]),
        )
        return Reply(
            f"{texts.OVERVIEW_TITLE}\n{body}",
            "overview",
            image=image,
            in_group=bool(row["active_booking"]),
        )

    async def show_overview(self, user_id: int) -> Reply:
        async def body(db: asyncpg.Pool) -> Reply:
            return await self._overview(db, user_id)

        return await self._run(user_id, body)

    async def _set_group(self, user_id: int, active: bool) -> Reply:
        async def body(db: asyncpg.Pool) -> Reply:
            updated = await update_active_booking(
                db, discord_user_id=user_id, active_booking=active
            )
            if not updated:
                return Reply(texts.NO_PROFILE, "info")
            await self._set_invites_role(db, user_id, add=active)
            return await self._overview(db, user_id)

        return await self._run(user_id, body)

    async def leave_group(self, user_id: int) -> Reply:
        return await self._set_group(user_id, False)

    async def join_group(self, user_id: int) -> Reply:
        return await self._set_group(user_id, True)

    # --- Discord helpers (best effort) ---

    def _guilds(self, guild_id: int | None) -> list[disnake.Guild]:
        client = self._client
        if client is None:
            return []
        if guild_id:
            guild = client.get_guild(guild_id)
            if guild is not None:
                return [guild]
        return list(client.guilds)

    def _member(self, user_id: int, guild_id: int | None) -> tuple[Any, Any]:
        for guild in self._guilds(guild_id):
            member = guild.get_member(user_id)
            if member is not None:
                return guild, member
        return None, None

    def _discord_name(self, user_id: int, guild_id: int | None, fallback: str) -> str:
        try:
            _, member = self._member(user_id, guild_id)
            if member is not None:
                return member.display_name
        except Exception:
            logger.warning("Member lookup failed for %s", user_id, exc_info=True)
        return fallback

    async def _set_invites_role(
        self, db: asyncpg.Pool, user_id: int, *, add: bool, guild_id: int | None = None
    ) -> None:
        try:
            guild, member = self._member(user_id, guild_id)
            if member is None:
                logger.warning("Member %s not found; invites role unchanged", user_id)
                return
            role = await get_invites_role(db, guild)
            if role is None:
                logger.warning("Invites role not found in guild %s", guild.id)
                return
            if add:
                await member.add_roles(role, reason="DJ joined the Active DJ Booking Group")
            else:
                await member.remove_roles(role, reason="DJ left the Active DJ Booking Group")
        except Exception:
            logger.warning("Could not change the invites role of %s", user_id, exc_info=True)
