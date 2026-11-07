"""Views of the DJ onboarding conversation and the "My DJ profile" overview.

The DjConversationService (ctx.dj_conversations) holds all state and decides what
to say; these views only turn its Reply into a DM message with the matching
buttons and pass button presses back to it. Every view is persistent (fixed
custom ids, timeout=None) and registered once in main.py via `registered_views`,
so buttons of messages sent before a restart keep working. Conversation buttons
carry no state: the service compares the pressed message id with the stored one
and answers STALE_BUTTON for an outdated message. Overview buttons act on
`inter.author.id` without a stale check.
"""
import io
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import disnake

from bot.services.dj_conversation import STEPS, Reply
from bot.ui import texts
from bot.ui.gates import GatedView

if TYPE_CHECKING:
    from bot.context import BotContext
    from bot.services.dj_conversation import DjConversationService

logger = logging.getLogger(__name__)

LOGO_FILENAME = "logo.png"
_RECORDED_KINDS = frozenset({"question", "summary"})

ServiceCall = Callable[["DjConversationService"], Awaitable[Reply]]


def _service(ctx: "BotContext") -> "DjConversationService | None":
    return getattr(ctx, "dj_conversations", None)


def _guild_id(inter: disnake.MessageInteraction) -> int:
    """Id of the first guild of the bot that has the user as a member, else 0."""
    for guild in getattr(inter.client, "guilds", None) or []:
        try:
            if guild.get_member(inter.author.id) is not None:
                return guild.id
        except Exception:
            logger.warning("Member lookup failed for %s", inter.author.id, exc_info=True)
    return 0


def _question_view(ctx: "BotContext", reply: Reply) -> "QuestionView":
    return QuestionView(
        ctx, skip=reply.allow_skip, as_is=reply.allow_as_is, remove=reply.allow_remove
    )


def _view_for(ctx: "BotContext", reply: Reply) -> disnake.ui.View | None:
    if reply.kind == "summary":
        return SummaryView(ctx)
    if reply.kind == "overview":
        return OverviewView(ctx, in_group=reply.in_group)
    if reply.kind == "question" or (reply.kind == "error" and reply.step in STEPS):
        # An error about the open question keeps that question's buttons.
        return _question_view(ctx, reply)
    return None


def _records_message(reply: Reply) -> bool:
    """Messages with conversation buttons become the conversation's current message."""
    return reply.kind in _RECORDED_KINDS or (reply.kind == "error" and reply.step in STEPS)


async def _send(channel_or_inter: Any, text: str, record: bool, kwargs: dict[str, Any]) -> Any:
    """Send one message; return it when it is needed for recording (else may be None)."""
    if isinstance(channel_or_inter, disnake.abc.Messageable):
        return await channel_or_inter.send(text, **kwargs)
    if channel_or_inter.response.is_done():
        return await channel_or_inter.followup.send(text, wait=True, **kwargs)
    await channel_or_inter.response.send_message(text, **kwargs)
    return await channel_or_inter.original_response() if record else None


async def send_reply(
    ctx: "BotContext", channel_or_inter: Any, user_id: int, reply: Reply
) -> None:
    """Send a Reply as a DM message with its buttons (and logo). Never raises on HTTP errors.

    `channel_or_inter` is a messageable channel (channel.send) or an interaction
    (response.send_message, or followup.send once the response is done). Messages
    that carry conversation buttons are recorded as the conversation's message.
    If a message with the logo fails, it is sent once more without the logo, so the
    DJ always gets the text and the buttons.
    """
    kwargs: dict[str, Any] = {}
    view = _view_for(ctx, reply)
    if view is not None:
        kwargs["view"] = view
    record = _records_message(reply)
    try:
        if reply.image is None:
            sent = await _send(channel_or_inter, reply.text, record, kwargs)
        else:
            try:
                file = disnake.File(io.BytesIO(reply.image), filename=LOGO_FILENAME)
                sent = await _send(channel_or_inter, reply.text, record, {**kwargs, "file": file})
            except disnake.HTTPException:
                logger.warning(
                    "Could not send the logo to %s; sending without it", user_id, exc_info=True
                )
                sent = await _send(channel_or_inter, reply.text, record, kwargs)
    except disnake.HTTPException:
        logger.warning("Could not send the DJ conversation message to %s", user_id, exc_info=True)
        return
    service = _service(ctx)
    if record and sent is not None and service is not None:
        await service.record_message_id(user_id, sent.id)


async def respond(
    ctx: "BotContext",
    inter: disnake.MessageInteraction,
    call: ServiceCall,
    *,
    defer: bool = True,
) -> None:
    """Run one service call for a button press and answer with its Reply.

    With `defer` the interaction is acknowledged first, so slow saves (Notion sync,
    role changes) cannot run past Discord's response deadline.
    """
    service = _service(ctx)
    if service is None:
        await send_reply(ctx, inter, inter.author.id, Reply(texts.GENERIC_ERROR, "error"))
        return
    if defer:
        try:
            await inter.response.defer()
        except disnake.HTTPException:
            logger.warning("Could not acknowledge the button of %s", inter.author.id, exc_info=True)
            return
    reply = await call(service)
    await send_reply(ctx, inter, inter.author.id, reply)


class OnboardingIntroView(GatedView):
    """Welcome DM buttons: join the DJ pool (starts the conversation) or not now."""

    def __init__(self, ctx: "BotContext") -> None:
        super().__init__(timeout=None)
        self.ctx = ctx

    @disnake.ui.button(
        label=texts.BTN_JOIN_POOL,
        style=disnake.ButtonStyle.success,
        custom_id="onboarding:go_active",
    )
    async def go_active(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id = inter.author.id
        guild_id = _guild_id(inter)
        await respond(self.ctx, inter, lambda s: s.start_onboarding(user_id, guild_id))

    @disnake.ui.button(
        label=texts.BTN_NOT_NOW,
        style=disnake.ButtonStyle.secondary,
        custom_id="onboarding:pass_for_now",
    )
    async def pass_for_now(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id = inter.author.id
        if _service(self.ctx) is None:
            await send_reply(self.ctx, inter, user_id, Reply(texts.ONBOARD_DECLINED, "info"))
            return
        # Ends an open onboarding, so the next DM opens the menu as the text says.
        await respond(self.ctx, inter, lambda s: s.decline(user_id), defer=False)


class QuestionView(GatedView):
    """Buttons of an open question: only the ones the Reply allows, plus Cancel."""

    def __init__(self, ctx: "BotContext", *, skip: bool, as_is: bool, remove: bool) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        if not skip:
            self.remove_item(self.skip_button)
        if not as_is:
            self.remove_item(self.use_as_is_button)
        if not remove:
            self.remove_item(self.remove_button)

    @disnake.ui.button(
        label=texts.BTN_SKIP, style=disnake.ButtonStyle.secondary, custom_id="dj:skip"
    )
    async def skip_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id, message_id = inter.author.id, inter.message.id
        await respond(self.ctx, inter, lambda s: s.skip(user_id, message_id))

    @disnake.ui.button(
        label=texts.BTN_USE_AS_IS, style=disnake.ButtonStyle.primary, custom_id="dj:use_as_is"
    )
    async def use_as_is_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id, message_id = inter.author.id, inter.message.id
        await respond(self.ctx, inter, lambda s: s.use_as_is(user_id, message_id))

    @disnake.ui.button(
        label=texts.BTN_REMOVE, style=disnake.ButtonStyle.danger, custom_id="dj:remove"
    )
    async def remove_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id, message_id = inter.author.id, inter.message.id
        await respond(self.ctx, inter, lambda s: s.remove_value(user_id, message_id))

    @disnake.ui.button(
        label=texts.BTN_CANCEL_SETUP, style=disnake.ButtonStyle.secondary, custom_id="dj:cancel"
    )
    async def cancel_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id, message_id = inter.author.id, inter.message.id
        await respond(self.ctx, inter, lambda s: s.cancel(user_id, message_id))


class SummaryView(GatedView):
    """Summary buttons: Looks good (saves), Start over, Cancel."""

    def __init__(self, ctx: "BotContext") -> None:
        super().__init__(timeout=None)
        self.ctx = ctx

    @disnake.ui.button(
        label=texts.BTN_LOOKS_GOOD, style=disnake.ButtonStyle.success, custom_id="dj:confirm"
    )
    async def confirm_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id, message_id = inter.author.id, inter.message.id
        await respond(self.ctx, inter, lambda s: s.confirm(user_id, message_id))

    @disnake.ui.button(
        label=texts.BTN_START_OVER, style=disnake.ButtonStyle.secondary, custom_id="dj:start_over"
    )
    async def start_over_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id, message_id = inter.author.id, inter.message.id
        await respond(self.ctx, inter, lambda s: s.start_over(user_id, message_id))

    @disnake.ui.button(
        label=texts.BTN_CANCEL_SETUP, style=disnake.ButtonStyle.secondary, custom_id="dj:cancel"
    )
    async def cancel_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id, message_id = inter.author.id, inter.message.id
        await respond(self.ctx, inter, lambda s: s.cancel(user_id, message_id))


class OverviewView(GatedView):
    """"My DJ profile" buttons: one per field, plus leave or join the booking group.

    Stateless: every button acts on the pressing user, old overviews keep working.
    `all_buttons` keeps both group buttons (persistent registration only).
    """

    def __init__(self, ctx: "BotContext", *, in_group: bool, all_buttons: bool = False) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        if not all_buttons:
            self.remove_item(self.join_button if in_group else self.leave_button)

    async def _edit(self, inter: disnake.MessageInteraction, field: str) -> None:
        user_id = inter.author.id
        guild_id = _guild_id(inter)
        await respond(self.ctx, inter, lambda s: s.start_edit(user_id, guild_id, field))

    @disnake.ui.button(
        label=texts.BTN_CHANGE_NAME, style=disnake.ButtonStyle.secondary,
        custom_id="dj:edit:name", row=0,
    )
    async def edit_name(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await self._edit(inter, "name")

    @disnake.ui.button(
        label=texts.BTN_CHANGE_VRCHAT, style=disnake.ButtonStyle.secondary,
        custom_id="dj:edit:vrchat", row=0,
    )
    async def edit_vrchat(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        await self._edit(inter, "vrchat")

    @disnake.ui.button(
        label=texts.BTN_CHANGE_LOGO, style=disnake.ButtonStyle.secondary,
        custom_id="dj:edit:logo", row=0,
    )
    async def edit_logo(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await self._edit(inter, "logo")

    @disnake.ui.button(
        label=texts.BTN_CHANGE_LINK, style=disnake.ButtonStyle.secondary,
        custom_id="dj:edit:link", row=1,
    )
    async def edit_link(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await self._edit(inter, "link")

    @disnake.ui.button(
        label=texts.BTN_CHANGE_ALT_LINK, style=disnake.ButtonStyle.secondary,
        custom_id="dj:edit:alt_link", row=1,
    )
    async def edit_alt_link(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        await self._edit(inter, "alt_link")

    @disnake.ui.button(
        label=texts.BTN_LEAVE_GROUP, style=disnake.ButtonStyle.secondary,
        custom_id="dj:leave", row=1,
    )
    async def leave_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id = inter.author.id
        await respond(self.ctx, inter, lambda s: s.leave_group(user_id))

    @disnake.ui.button(
        label=texts.BTN_JOIN_GROUP, style=disnake.ButtonStyle.success,
        custom_id="dj:join", row=1,
    )
    async def join_button(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        user_id = inter.author.id
        await respond(self.ctx, inter, lambda s: s.join_group(user_id))


def registered_views(ctx: "BotContext") -> list[disnake.ui.View]:
    """One instance per view with every custom id exactly once, for `bot.add_view`.

    dj:cancel is registered through the QuestionView only; its handler is the same
    for question and summary messages.
    """
    summary = SummaryView(ctx)
    summary.remove_item(summary.cancel_button)
    return [
        OnboardingIntroView(ctx),
        QuestionView(ctx, skip=True, as_is=True, remove=True),
        summary,
        OverviewView(ctx, in_group=True, all_buttons=True),
    ]
