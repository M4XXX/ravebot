"""discord.ui.View subclasses — implemented across Story 2.x–5.x.

Views: OnboardingIntroView (Story 2.1), OnboardingModal + RetryOnboardingModalView (Story 2.2),
       BookingConfirmView (Story 4.2), SlotSelectionView (Story 4.1),
       EventSelectionView (Story 4.1), CancellationView, CancelConfirmView, FallbackMenuView,
       OutreachView (Story 5.2).

RULE: Every View subclass MUST:
  1. Accept BotContext as a constructor argument
  2. Implement on_timeout() — re-present flow or send warm timeout message
  3. Acquire user's asyncio.Lock on entry; release on completion, abandonment, and in on_timeout()
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import disnake

from bot.db.djs import get_dj_by_discord_id, insert_dj, set_notion_dj_page_id, update_active_booking
from bot.ui import texts
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    from bot.context import BotContext
    from bot.notion.models import EventInfo

logger = logging.getLogger(__name__)


def _is_valid_url(value: str) -> bool:
    """Return True if value looks like an HTTP/HTTPS URL."""
    return bool(value) and value.startswith(("http://", "https://")) and len(value) > 10


class OnboardingIntroView(disnake.ui.View):
    """Intro DM view — two buttons sent when DJ role is first detected.

    Buttons:
      [✅ Go Active]   — success style — opens onboarding modal (Story 2.2)
      [Pass for now]   — secondary style — warm decline (Story 2.4)

    timeout=None: DJ-facing flows never expire mid-flow (UX-DR16).
    Lock acquisition deferred to button handlers (Story 2.2/2.4).

    [Source: epics.md#Story 2.1 AC2]
    [Source: ux-design-specification.md#UX-DR13, UX-DR16]
    """

    def __init__(self, ctx: "BotContext", user_id: int, guild_id: int = 0) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        self.user_id = user_id
        self.guild_id = guild_id

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.

        Required by View lifecycle contract.
        [Source: architecture.md#View Lifecycle Contract]
        """

    @disnake.ui.button(
        label=texts.BTN_JOIN_POOL,
        style=disnake.ButtonStyle.success,
        custom_id="onboarding:go_active",
    )
    async def go_active(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Acquire per-user lock and open the onboarding modal.

        [Source: epics.md#Story 2.2 AC1, AC5]
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        # Story 6.1 AC3: log button tap
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        lock = self.ctx.get_user_lock(inter.author.id)
        if lock.locked():
            await inter.response.send_message(
                texts.FLOW_OPEN,
                ephemeral=True,
            )
            return
        await lock.acquire()
        await inter.response.send_modal(
            OnboardingModal(self.ctx, inter.author.id, lock, self.guild_id)
        )

    @disnake.ui.button(
        label=texts.BTN_NOT_NOW,
        style=disnake.ButtonStyle.secondary,
        custom_id="onboarding:pass_for_now",
    )
    async def pass_for_now(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Warm decline — no DB write, no role assignment (Story 2.4 AC1)."""
        # Story 6.1 AC3: log button tap
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        response_text = texts.ONBOARD_DECLINED
        await inter.response.send_message(response_text)
        # Story 6.1 AC1: log bot response
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="BOT",
                content=response_text,
            )


class OnboardingModal(disnake.ui.Modal):
    """Profile setup modal — opens when DJ taps [Go Active] in the onboarding DM.

    5 fields: DJ Name (required), VRChat Name (optional), Logo URL (optional),
    Primary Stream URL (required), Backup Stream URL (optional).

    Lock management:
    - Lock is ALREADY HELD when this modal is presented (acquired in go_active).
    - callback() releases the lock on ALL exit paths (success, validation failure, db guard).

    [Source: epics.md#Story 2.2 AC1, AC2, AC4]
    [Source: ux-design-specification.md#UX-DR15]
    [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
    """

    def __init__(
        self, ctx: "BotContext", user_id: int, lock: asyncio.Lock, guild_id: int = 0
    ) -> None:
        self._ctx = ctx
        self._user_id = user_id
        self._lock = lock
        self._guild_id = guild_id
        super().__init__(
            title="Set Up Your Profile",
            custom_id="onboarding_modal",
            components=[
                disnake.ui.TextInput(
                    label="DJ Name",
                    placeholder="Your DJ name as shown on posters",
                    custom_id="dj_name",
                    style=disnake.TextInputStyle.short,
                    min_length=1,
                    max_length=100,
                    required=True,
                ),
                disnake.ui.TextInput(
                    label="VRChat Username",
                    placeholder="Your VRChat username (optional, skip if not applicable)",
                    custom_id="vrchat_name",
                    style=disnake.TextInputStyle.short,
                    max_length=100,
                    required=False,
                ),
                disnake.ui.TextInput(
                    label="Logo URL",
                    placeholder="https://i.imgur.com/yourlogo.png (optional)",
                    custom_id="logo_url",
                    style=disnake.TextInputStyle.short,
                    max_length=512,
                    required=False,
                ),
                disnake.ui.TextInput(
                    label="Primary Stream URL",
                    placeholder="https://twitch.tv/yourname",
                    custom_id="stream_url_primary",
                    style=disnake.TextInputStyle.short,
                    max_length=512,
                    required=True,
                ),
                disnake.ui.TextInput(
                    label="Backup Stream URL",
                    placeholder="https://twitch.tv/yourname (optional, skip if not applicable)",
                    custom_id="stream_url_backup",
                    style=disnake.TextInputStyle.short,
                    max_length=512,
                    required=False,
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        """Process modal submission: validate URLs → insert DJ → confirm → release lock.

        Lock is released on ALL exit paths.
        Story 2.3 will extend this to add Discord role assignment and Notion sync
        BEFORE the confirmation message is sent.

        [Source: epics.md#Story 2.2 AC2, AC4]
        """
        values = inter.text_values
        dj_name = values["dj_name"].strip()
        vrchat_name = values["vrchat_name"].strip() or None
        logo_url = values["logo_url"].strip() or None
        stream_url_primary = values["stream_url_primary"].strip()
        stream_url_backup = values["stream_url_backup"].strip() or None

        # Validate optional URL fields (required fields validated by Discord natively)
        invalid_fields: list[str] = []
        if logo_url and not _is_valid_url(logo_url):
            invalid_fields.append("Logo URL")
        if not _is_valid_url(stream_url_primary):
            invalid_fields.append("Primary Stream URL")
        if stream_url_backup and not _is_valid_url(stream_url_backup):
            invalid_fields.append("Backup Stream URL")

        if invalid_fields:
            self._lock.release()
            await inter.response.send_message(
                texts.link_invalid(", ".join(invalid_fields)),
                view=RetryOnboardingModalView(self._ctx, inter.author.id, self._guild_id),
            )
            return

        # Guard: DB pool should always be available at this point,
        # but guard defensively in case of startup race condition.
        if self._ctx.db is None:
            self._lock.release()
            if self._ctx.log_service is not None:
                await self._ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_DB_UNAVAILABLE,
                    trigger=f"OnboardingModal.callback: db pool is None for user {self._user_id}",
                    action_taken="Lock released; DJ informed to retry.",
                    manual_action="Check DB pool initialisation in bot/main.py.",
                )
            await inter.response.send_message(
                texts.GENERIC_ERROR,
            )
            return

        await insert_dj(
            self._ctx.db,
            discord_user_id=inter.author.id,
            dj_name=dj_name,
            vrchat_name=vrchat_name,
            logo_url=logo_url,
            stream_url_primary=stream_url_primary,
            stream_url_backup=stream_url_backup,
        )
        self._lock.release()

        # Story 2.3: assign Active Booking role
        guild = inter.client.get_guild(self._guild_id)
        if guild is not None:
            member = guild.get_member(inter.author.id)
            if member is not None:
                role = disnake.utils.get(guild.roles, name="Active Booking")
                if role is not None:
                    try:
                        await member.add_roles(role, reason="Onboarding completed")
                    except disnake.Forbidden:
                        logger.warning(
                            "Missing Manage Roles permission to assign Active Booking role "
                            "to user %s in guild %s",
                            inter.author.id,
                            self._guild_id,
                        )
                    except disnake.HTTPException as exc:
                        logger.warning(
                            "HTTP error assigning Active Booking role to user %s in guild %s: %s",
                            inter.author.id,
                            self._guild_id,
                            exc,
                        )
                else:
                    logger.warning("Active Booking role not found in guild %s", self._guild_id)
            else:
                logger.warning(
                    "Member %s not in guild cache %s — cannot assign role",
                    inter.author.id,
                    self._guild_id,
                )
        else:
            logger.warning("Guild %s not in bot cache — cannot assign role", self._guild_id)

        # Story 2.3 / 4.2: Notion DJ record sync + store returned page ID (best-effort)
        if self._ctx.notion_client is not None:
            try:
                notion_page_id = await self._ctx.notion_client.upsert_dj(
                    inter.author.id,
                    dj_name,
                    inter.author.display_name or inter.author.name,
                    vrchat_name=vrchat_name,
                    logo_url=logo_url,
                    link=stream_url_primary,
                    alt_link=stream_url_backup,
                )
                # Story 4.2: persist the Notion page ID so BookingConfirmView can later
                # append this DJ to the event's DJs relation
                if self._ctx.db is not None:
                    await set_notion_dj_page_id(
                        self._ctx.db,
                        inter.author.id,
                        notion_page_id,
                    )
            except Exception:
                logger.exception(
                    "Unexpected error during Notion DJ record sync for user %s",
                    inter.author.id,
                )

        await inter.response.send_message(texts.ONBOARD_DONE)


class RetryOnboardingModalView(disnake.ui.View):
    """Single-button view shown after URL validation failure.

    Allows DJ to re-open the onboarding modal without restarting from scratch.
    Re-acquires the user lock before opening the modal (was released on failure).

    timeout=None: no time pressure on retry (UX-DR16).
    [Source: epics.md#Story 2.2 AC4]
    [Source: ux-design-specification.md#UX-DR16]
    """

    def __init__(self, ctx: "BotContext", user_id: int, guild_id: int = 0) -> None:
        super().__init__(timeout=None)
        self._ctx = ctx
        self._user_id = user_id
        self._guild_id = guild_id

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.
        [Source: architecture.md#View Lifecycle Contract]
        """

    @disnake.ui.button(
        label=texts.BTN_TRY_AGAIN,
        style=disnake.ButtonStyle.primary,
        custom_id="onboarding:retry_modal",
    )
    async def retry(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Re-open the onboarding modal. Re-acquires the per-user lock."""
        # Story 6.1 AC3: log button tap
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        lock = self._ctx.get_user_lock(inter.author.id)
        if lock.locked():
            await inter.response.send_message(
                texts.FLOW_OPEN,
                ephemeral=True,
            )
            return
        await lock.acquire()
        await inter.response.send_modal(
            OnboardingModal(self._ctx, inter.author.id, lock, self._guild_id)
        )


class FallbackMenuView(disnake.ui.View):
    """Fallback help menu — responds to all unrecognised free-text DMs.

    Shown when a DJ sends free text outside any active multi-step flow.
    NOT shown during active onboarding (on_message checks the lock first).

    Buttons: Book a slot, Cancel a slot, Show lineup and exactly one pool button:
    "Contact me for Booking" (active False/None) or "Don't automatically ask for
    booking" (active True). The other pool button is removed in __init__.

    timeout=None: DJ-facing views never expire (UX-DR16).
    No per-user lock: Go Active/Go Inactive are single-step interactions.

    [Source: epics.md#Story 2.4 AC2-AC4]
    [Source: ux-design-specification.md#UX-DR1, UX-DR13, UX-DR16]
    """

    def __init__(self, ctx: "BotContext", active: bool | None = None) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        if active:
            self.remove_item(self.go_active)
        else:
            self.remove_item(self.go_inactive)

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.
        [Source: architecture.md#View Lifecycle Contract]
        """

    @disnake.ui.button(
        label=texts.BTN_BOOK,
        style=disnake.ButtonStyle.primary,
        custom_id="fallback:book_slot",
        row=0,
    )
    async def book_slot(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Entry point for the slot booking flow.

        Acquires per-user lock, fetches events from Notion, filters to events
        with open slots, and presents them as an EventSelectionView.

        [Source: epics.md#Story 4.1 AC1, AC3, AC4, AC6]
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        # Story 6.1 AC3: log button tap
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        start = await prepare_booking(self.ctx, inter.author.id)
        if start.error is not None:
            await inter.response.send_message(start.error, ephemeral=True)
            return

        content = start.content
        await inter.response.send_message(content, view=start.view, ephemeral=True)
        # Story 6.1 AC1: log bot response
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="BOT",
                content=content,
            )

    @disnake.ui.button(
        label=texts.BTN_CANCEL,
        style=disnake.ButtonStyle.secondary,
        custom_id="fallback:cancel_slot",
        row=0,
    )
    async def cancel_slot(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Self-service cancellation flow entry point. (Story 4.4)

        [Source: epics.md#Story 4.4 AC1, AC2, AC6]
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        # Story 6.1 AC3: log button tap
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        from bot.db.bookings import get_dj_slot_locks

        user_id = inter.author.id
        lock = self.ctx.get_user_lock(user_id)

        # AC6: Per-user lock guard — same pattern as book_slot
        if lock.locked():
            await inter.response.send_message(
                texts.FLOW_OPEN,
                ephemeral=True,
            )
            return
        await lock.acquire()

        # Guard: DB must be available
        if self.ctx.db is None:
            lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        # Guard: Notion client must be available
        if self.ctx.notion_client is None:
            lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        # Query DJ's current slot locks (authoritative source)
        try:
            slot_lock_rows = await get_dj_slot_locks(self.ctx.db, user_id)
        except Exception:
            logger.exception("cancel_slot: get_dj_slot_locks failed")
            lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        if not slot_lock_rows:
            lock.release()
            await inter.response.send_message(
                texts.NO_BOOKINGS,
                ephemeral=True,
            )
            return

        # Fetch Notion events to get names and slot times
        try:
            events = await self.ctx.notion_client.list_events()
        except Exception:
            logger.exception("cancel_slot: list_events() failed")
            lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        # Build bookings list by matching slot_lock rows to Notion events
        events_by_id = {e.page_id: e for e in events}
        bookings = []
        for row in slot_lock_rows:
            event = events_by_id.get(row["event_id"])
            if event is None:
                continue  # event no longer in future — skip
            slot_index = row["slot_index"]
            slot_unix = int(event.slot_start(slot_index).timestamp())
            bookings.append({"event": event, "slot_index": slot_index, "slot_unix": slot_unix})

        if not bookings:
            lock.release()
            await inter.response.send_message(
                texts.NO_BOOKINGS,
                ephemeral=True,
            )
            return

        view = CancellationView(self.ctx, bookings, lock, user_id)
        cancel_prompt = texts.CANCEL_WHICH
        await inter.response.send_message(cancel_prompt, view=view, ephemeral=True)
        # Story 6.1 AC1: log bot response
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="BOT",
                content=cancel_prompt,
            )

    @disnake.ui.button(
        label=texts.BTN_INVITES_ON,
        style=disnake.ButtonStyle.success,
        custom_id="fallback:go_active",
        row=1,
    )
    async def go_active(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Set active_booking=True + assign Active Booking role.

        [Source: epics.md#Story 2.4 AC3]
        """
        # Story 6.1 AC3: log button tap
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        if self.ctx.db is None:
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        updated = await update_active_booking(
            self.ctx.db,
            discord_user_id=inter.author.id,
            active_booking=True,
        )

        if not updated:
            await inter.response.send_message(texts.NO_PROFILE, ephemeral=True)
            return

        # Assign Active Booking role (best-effort — failure does not block confirmation)
        for guild in inter.client.guilds:
            member = guild.get_member(inter.author.id)
            if member is not None:
                role = disnake.utils.get(guild.roles, name="Active Booking")
                if role is not None:
                    try:
                        await member.add_roles(
                            role, reason="DJ opted back in via FallbackMenuView"
                        )
                    except (disnake.Forbidden, disnake.HTTPException) as exc:
                        logger.warning(
                            "Could not assign Active Booking role to user %s: %s",
                            inter.author.id,
                            exc,
                        )
                break

        go_active_text = texts.INVITES_ON_DONE
        await inter.response.send_message(go_active_text)
        # Story 6.1 AC1: log bot response
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="BOT",
                content=go_active_text,
            )

    @disnake.ui.button(
        label=texts.BTN_INVITES_OFF,
        style=disnake.ButtonStyle.secondary,
        custom_id="fallback:go_inactive",
        row=1,
    )
    async def go_inactive(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Set active_booking=False + remove Active Booking role.

        [Source: epics.md#Story 2.4 AC4]
        """
        # Story 6.1 AC3: log button tap
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        if self.ctx.db is None:
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        updated = await update_active_booking(
            self.ctx.db,
            discord_user_id=inter.author.id,
            active_booking=False,
        )
        if not updated:
            await inter.response.send_message(texts.NO_PROFILE, ephemeral=True)
            return

        # Remove Active Booking role (best-effort — failure does not block confirmation)
        for guild in inter.client.guilds:
            member = guild.get_member(inter.author.id)
            if member is not None:
                role = disnake.utils.get(guild.roles, name="Active Booking")
                if role is not None:
                    try:
                        await member.remove_roles(
                            role, reason="DJ opted out via FallbackMenuView"
                        )
                    except (disnake.Forbidden, disnake.HTTPException) as exc:
                        logger.warning(
                            "Could not remove Active Booking role from user %s: %s",
                            inter.author.id,
                            exc,
                        )
                break

        go_inactive_text = texts.INVITES_OFF_DONE
        await inter.response.send_message(go_inactive_text)
        # Story 6.1 AC1: log bot response
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="BOT",
                content=go_inactive_text,
            )

    @disnake.ui.button(
        label=texts.BTN_LINEUP,
        style=disnake.ButtonStyle.primary,
        custom_id="fallback:show_lineup",
        row=2,
    )
    async def show_lineup(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Show the first upcoming event's lineup via LineupUpdateEmbed.

        Uses list_events() to fetch live Notion data. Falls back gracefully
        when Notion is unavailable or returns no events.

        [Source: epics.md#Story 3.3 AC1, AC5, AC6]
        """
        # Story 6.1 AC3: log button tap
        if self.ctx.log_service is not None:
            await self.ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        from bot.services.lineup import _today, future_events, upcoming_event
        from bot.ui.embeds import LineupUpdateEmbed

        if self.ctx.notion_client is None:
            await inter.response.send_message(texts.GENERIC_ERROR, ephemeral=True)
            return

        try:
            events = await self.ctx.notion_client.list_events()
        except Exception:
            logger.exception("show_lineup: list_events() failed")
            await inter.response.send_message(texts.GENERIC_ERROR, ephemeral=True)
            return

        today = _today()
        chosen = upcoming_event(events, today)
        if chosen is None:
            await inter.response.send_message(texts.NO_LINEUP, ephemeral=True)
            return

        upcoming = future_events(events, today)
        pick_view = LineupEventPickView(upcoming) if len(upcoming) > 1 else None
        kwargs: dict = {"embed": LineupUpdateEmbed(chosen), "ephemeral": True}
        if pick_view is not None:
            kwargs["view"] = pick_view
        await inter.response.send_message(**kwargs)


class LineupEventPickView(disnake.ui.View):
    """One button per future event (max 5); each replies privately with that lineup."""

    def __init__(self, events: list["EventInfo"]) -> None:
        super().__init__(timeout=None)
        for i, event in enumerate(events[:5]):
            btn = disnake.ui.Button(
                label=event.event_id[:80],
                style=disnake.ButtonStyle.secondary,
                custom_id=f"lineup_pick:{event.page_id}",
                row=0,
            )

            async def _cb(
                inter: disnake.MessageInteraction, e: "EventInfo" = event
            ) -> None:
                from bot.ui.embeds import LineupUpdateEmbed

                await inter.response.send_message(embed=LineupUpdateEmbed(e), ephemeral=True)

            btn.callback = _cb
            self.add_item(btn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_slot_indices(event: "EventInfo") -> list[int]:
    """Zero-based indices of open slots: the resolved row for index+1 holds no DJ."""
    rows = event.resolved_slots()
    return [
        i
        for i in range(event.slot_count)
        if rows.get(i + 1) is None or rows[i + 1].dj_page_id is None
    ]


MAX_SLOT_BUTTONS = 24  # Discord allows 25 components per message, one is Never mind


def _pick_slot_content(open_slot_indices: list[int]) -> str:
    """Slot picker message text; adds a note when buttons cannot show every open slot."""
    if len(open_slot_indices) > MAX_SLOT_BUTTONS:
        return f"{texts.PICK_SLOT}\n{texts.PICK_SLOT_TRUNCATED}"
    return texts.PICK_SLOT


def _filter_events_with_open_slots(events: list["EventInfo"]) -> list["EventInfo"]:
    """Return only events that have at least one open slot (see _open_slot_indices)."""
    return [event for event in events if _open_slot_indices(event)]


@dataclass
class BookingStart:
    """Result of prepare_booking: either an error text or the event picker."""

    error: str | None
    content: str
    view: "EventSelectionView | None"


async def prepare_booking(ctx: "BotContext", user_id: int) -> BookingStart:
    """Start the booking flow for a user.

    On error: returns the error text and holds no lock (a lock held by an already
    open flow is left untouched). On success the per-user lock is held and handed
    to the returned EventSelectionView.
    """
    lock = ctx.get_user_lock(user_id)
    if lock.locked():
        return BookingStart(texts.FLOW_OPEN, "", None)
    await lock.acquire()
    try:
        if ctx.notion_client is None:
            raise RuntimeError("notion_client is not configured")
        events = await ctx.notion_client.list_events()
        if not events:
            lock.release()
            return BookingStart(texts.NO_EVENTS, "", None)
        with_open = _filter_events_with_open_slots(events)
        if not with_open:
            lock.release()
            return BookingStart(texts.ALL_BOOKED, "", None)
        # Cap to 5 events (Discord limit: 5 buttons per row)
        with_open = with_open[:5]
        lines = [texts.PICK_EVENT]
        for i, event in enumerate(with_open, 1):
            lines.append(texts.event_line(i, event, len(_open_slot_indices(event))))
        view = EventSelectionView(ctx, with_open, lock, user_id)
    except Exception:
        logger.exception("prepare_booking failed")
        if lock.locked():
            lock.release()
        return BookingStart(texts.GENERIC_ERROR, "", None)
    return BookingStart(None, "\n".join(lines), view)


class LineupPostView(disnake.ui.View):
    """Persistent view under the Lineup Update post: one Book a slot button.

    The DM goes to the clicking user; the channel only gets ephemeral replies.
    """

    def __init__(self, ctx: "BotContext") -> None:
        super().__init__(timeout=None)
        self.ctx = ctx

    @disnake.ui.button(
        label=texts.BTN_BOOK_POST,
        style=disnake.ButtonStyle.primary,
        custom_id="lineup:book_slot",
    )
    async def book_slot(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        await inter.response.defer(ephemeral=True)
        user = inter.author
        if not await self._is_onboarded(user.id):
            await self._dm_or_report(inter, texts.NO_PROFILE, None, release_lock=False)
            return
        start = await prepare_booking(self.ctx, user.id)
        if start.error is not None:
            await inter.followup.send(start.error, ephemeral=True)
            return
        await self._dm_or_report(inter, start.content, start.view, release_lock=True)

    async def _is_onboarded(self, user_id: int) -> bool:
        """True when a DJ record exists. A lookup failure counts as onboarded so the
        booking flow's own checks decide."""
        try:
            return await get_dj_by_discord_id(self.ctx.db, user_id) is not None
        except Exception:
            logger.exception("LineupPostView: DJ lookup failed for %s", user_id)
            return True

    async def _dm_or_report(
        self,
        inter: disnake.MessageInteraction,
        content: str,
        view: disnake.ui.View | None,
        *,
        release_lock: bool,
    ) -> None:
        """DM the user, then reply privately; blocked or failed DMs are reported."""
        user = inter.author
        kwargs = {"view": view} if view is not None else {}
        try:
            await user.send(content, **kwargs)
        except disnake.Forbidden:
            if release_lock:
                self.ctx.get_user_lock(user.id).release()
            await inter.followup.send(texts.POST_DM_BLOCKED, ephemeral=True)
            if self.ctx.log_service is not None:
                await self.ctx.log_service.post_error(
                    ErrorCode.ERR_DM_FAILED,
                    trigger=(
                        f"{user.name} pressed 'Book a slot' on the Lineup Update "
                        "but I can't DM them."
                    ),
                    action_taken="Told them privately to allow DMs.",
                    manual_action=(
                        "They need to allow DMs from server members (Discord Settings, "
                        "Privacy & Safety), then press the button again."
                    ),
                )
            return
        except Exception:
            logger.exception("LineupPostView: DM to %s failed", user.id)
            if release_lock:
                self.ctx.get_user_lock(user.id).release()
            await inter.followup.send(texts.GENERIC_ERROR, ephemeral=True)
            return
        await inter.followup.send(texts.POST_DM_SENT, ephemeral=True)


# ---------------------------------------------------------------------------
# Story 4.1: EventSelectionView
# ---------------------------------------------------------------------------

class EventSelectionView(disnake.ui.View):
    """Event selection — one button per upcoming event with open slots.

    Presented after DJ taps [Book a Slot] from FallbackMenuView.
    The per-user lock is ALREADY HELD when this view is created
    (acquired in FallbackMenuView.book_slot).

    Buttons: one per event (secondary style, max 4 on rows 0–3) +
             [Never mind] (secondary, row 4).
    timeout=None: DJ-facing views never expire (UX-DR16).

    [Source: epics.md#Story 4.1 AC1]
    [Source: ux-design-specification.md#UX-DR13, UX-DR16]
    [Source: architecture.md#View Lifecycle Contract]
    [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
    """

    def __init__(
        self,
        ctx: "BotContext",
        events: list["EventInfo"],
        lock: asyncio.Lock,
        user_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self._ctx = ctx
        self._lock = lock
        self._user_id = user_id

        for i, event in enumerate(events):
            btn = disnake.ui.Button(
                label=event.event_id[:80],
                style=disnake.ButtonStyle.secondary,
                custom_id=f"event_select:{event.page_id}",
                row=min(i, 3),
            )

            async def _cb(
                inter: disnake.MessageInteraction,
                e: "EventInfo" = event,
            ) -> None:
                await self._handle_event(inter, e)

            btn.callback = _cb
            self.add_item(btn)

        never_mind = disnake.ui.Button(
            label=texts.BTN_NEVER_MIND,
            style=disnake.ButtonStyle.secondary,
            custom_id="event_select:cancel",
            row=4,
        )
        never_mind.callback = self._handle_cancel
        self.add_item(never_mind)

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.
        [Source: architecture.md#View Lifecycle Contract]
        """

    async def _handle_cancel(self, inter: disnake.MessageInteraction) -> None:
        """[Never mind] — release lock and respond warmly. (AC5)"""
        if self._lock.locked():
            self._lock.release()
        await inter.response.send_message(texts.BOOKING_ABORTED, ephemeral=True)

    async def _handle_event(
        self,
        inter: disnake.MessageInteraction,
        event: "EventInfo",
    ) -> None:
        """Event selected — compute open slots and present SlotSelectionView. (AC2)

        [Source: epics.md#Story 4.1 AC2]
        """
        from bot.ui.embeds import LineupUpdateEmbed

        open_slot_indices = _open_slot_indices(event)

        embed = LineupUpdateEmbed(event)
        slot_view = SlotSelectionView(
            self._ctx,
            event,
            open_slot_indices,
            self._lock,
            self._user_id,
        )
        try:
            await inter.response.send_message(
                content=_pick_slot_content(open_slot_indices),
                embed=embed,
                view=slot_view,
                ephemeral=True,
            )
        except Exception:
            logger.exception("_handle_event: send_message failed")
            self._lock.release()


# ---------------------------------------------------------------------------
# Story 4.1: SlotSelectionView
# ---------------------------------------------------------------------------

class SlotSelectionView(disnake.ui.View):
    """Slot selection — one button per open slot in the chosen event.

    Presented alongside LineupUpdateEmbed after DJ selects an event in
    EventSelectionView. The per-user lock is ALREADY HELD when this view
    is created.

    Buttons: one per open slot index (secondary style) + [Never mind]
             (secondary, row 4).
    timeout=None: DJ-facing views never expire (UX-DR16).

    Slot button callback implemented in Story 4.2 — presents BookingConfirmView.

    [Source: epics.md#Story 4.1 AC2]
    [Source: ux-design-specification.md#UX-DR3, UX-DR16]
    [Source: architecture.md#View Lifecycle Contract]
    """

    def __init__(
        self,
        ctx: "BotContext",
        event: "EventInfo",
        open_slot_indices: list[int],
        lock: asyncio.Lock,
        user_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self._ctx = ctx
        self._event = event
        self._lock = lock
        self._user_id = user_id

        event_id = event.page_id

        for row_idx, slot_index in enumerate(open_slot_indices[:MAX_SLOT_BUTTONS]):
            btn = disnake.ui.Button(
                label=texts.slot_button(event, slot_index),
                style=disnake.ButtonStyle.secondary,
                custom_id=f"slot_select:{event_id}:{slot_index}",
                row=row_idx // 5,
            )

            async def _cb(
                inter: disnake.MessageInteraction,
                idx: int = slot_index,
            ) -> None:
                await self._handle_slot(inter, idx)

            btn.callback = _cb
            self.add_item(btn)

        never_mind = disnake.ui.Button(
            label=texts.BTN_NEVER_MIND,
            style=disnake.ButtonStyle.secondary,
            custom_id="slot_select:cancel",
            row=4,
        )
        never_mind.callback = self._handle_cancel
        self.add_item(never_mind)

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.
        [Source: architecture.md#View Lifecycle Contract]
        """

    async def _handle_cancel(self, inter: disnake.MessageInteraction) -> None:
        """[Never mind] — release lock and respond warmly. (AC5)"""
        if self._lock.locked():
            self._lock.release()
        await inter.response.send_message(texts.BOOKING_ABORTED, ephemeral=True)

    async def _handle_slot(
        self,
        inter: disnake.MessageInteraction,
        slot_index: int,
    ) -> None:
        """Slot selected — present BookingConfirmView for this slot. (Story 4.2)

        Computes the slot's Unix timestamp from the event start + slot_index slot lengths,
        then presents BookingConfirmEmbed + BookingConfirmView. The per-user lock remains
        held and is passed into BookingConfirmView (not released here).

        [Source: epics.md#Story 4.2 AC1]
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        from bot.ui.embeds import BookingConfirmEmbed

        slot_unix = int(self._event.slot_start(slot_index).timestamp())

        embed = BookingConfirmEmbed(self._event, slot_index)
        view = BookingConfirmView(
            ctx=self._ctx,
            event=self._event,
            slot_index=slot_index,
            slot_unix=slot_unix,
            lock=self._lock,
            user_id=self._user_id,
        )
        try:
            await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception:
            logger.exception("_handle_slot: send_message failed")
            if self._lock.locked():
                self._lock.release()


# ---------------------------------------------------------------------------
# Story 4.2: BookingConfirmView
# ---------------------------------------------------------------------------

class BookingConfirmView(disnake.ui.View):
    """Two-button confirmation view — confirms or cancels a pending slot booking.

    Presented alongside BookingConfirmEmbed after DJ selects a slot in
    SlotSelectionView. The per-user lock is ALREADY HELD when this view is created.

    Buttons:
      [✅ Confirm Booking] (success) — acquires PG slot lock → writes to Notion → confirms
      [✖ Cancel]           (secondary) — releases lock, sends warm dismissal

    Flow on confirm:
      1. Guard: ctx.db must be available
      2. acquire_slot_lock() via SELECT...FOR UPDATE
      3. If slot taken → warm message, release per-user lock (basic; Story 4.3 adds full race)
      4. get_notion_dj_page_id() for the DJ
      5. assign_slot() on the Notion Slots row
      6. On NotionWriteError → release_slot_lock() to undo DB claim → error msg → release lock
      7. On success → BookedEmbed → trigger lineup post → admin log → release lock

    timeout=None: DJ-facing views never expire (UX-DR16).

    [Source: epics.md#Story 4.2 AC1–AC6]
    [Source: ux-design-specification.md#UX-DR4, UX-DR5, UX-DR16]
    [Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
    [Source: architecture.md#View Lifecycle Contract]
    """

    def __init__(
        self,
        ctx: "BotContext",
        event: "EventInfo",
        slot_index: int,
        slot_unix: int,
        lock: asyncio.Lock,
        user_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self._ctx = ctx
        self._event = event
        self._slot_index = slot_index
        self._slot_unix = slot_unix
        self._lock = lock
        self._user_id = user_id

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.

        Required by View lifecycle contract. Lock would be released here if it fired.
        [Source: architecture.md#View Lifecycle Contract]
        """

    @disnake.ui.button(
        label=texts.BTN_CONFIRM,
        style=disnake.ButtonStyle.success,
        custom_id="booking_confirm:confirm",
    )
    async def confirm(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Acquire slot lock, write booking to Notion, send confirmation. (AC2–AC3)"""
        # Story 6.1 AC3: log button tap
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        await self._handle_confirm(inter)

    @disnake.ui.button(
        label=texts.BTN_BACK,
        style=disnake.ButtonStyle.secondary,
        custom_id="booking_confirm:cancel",
    )
    async def cancel(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Cancel the pending booking — release lock, warm dismissal. (AC4)"""
        # Story 6.1 AC3: log button tap
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        await self._handle_cancel(inter)

    async def _handle_cancel(self, inter: disnake.MessageInteraction) -> None:
        """[✖ Cancel] — release per-user lock and respond warmly. (AC4)"""
        if self._lock.locked():
            self._lock.release()
        await inter.response.send_message(
            texts.BOOKING_ABORTED,
            ephemeral=True,
        )

    async def _present_slot_taken(
        self, inter: disnake.MessageInteraction, trigger: str | None = None
    ) -> None:
        """Tell the DJ the slot was taken and offer the remaining open slots.

        Shared by a lost Postgres lock and an already-filled Notion slot row.
        Logs ERR_SLOT_CONFLICT best-effort; ``trigger`` overrides the log text
        (used when the Postgres lock holder is not the one to name).
        """
        event_id = self._event.page_id
        # AC2: Log ERR_SLOT_CONFLICT to admin channel — best-effort, never blocks DJ
        if self._ctx.log_service is not None:
            try:
                from bot.db.bookings import get_slot_lock_holder
                from bot.ui.embeds import ErrorCode
                if trigger is None:
                    winner_id = await get_slot_lock_holder(
                        self._ctx.db, event_id, self._slot_index
                    )
                    trigger = (
                        f"DJ {self._user_id} attempted slot {self._slot_index} in event "
                        f"{event_id!r}, but slot was already locked by DJ {winner_id}"
                    )
                await self._ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_SLOT_CONFLICT,
                    trigger=trigger,
                    action_taken="Presented updated slot selection to losing DJ",
                    manual_action="No manual action required.",
                )
            except Exception:
                logger.exception(
                    "BookingConfirmView: failed to post ERR_SLOT_CONFLICT"
                )

        # AC1: Get fresh event data from Notion — best-effort, fall back to stale self._event
        event_for_refresh = self._event
        if self._ctx.notion_client is not None:
            try:
                fresh_events = await self._ctx.notion_client.list_events()
                for fresh_event in fresh_events:
                    if fresh_event.page_id == event_id:
                        event_for_refresh = fresh_event
                        break
            except Exception:
                logger.exception(
                    "BookingConfirmView: failed to refresh events for slot conflict view"
                )

        # Compute remaining open slots from (possibly fresh) event data
        remaining = [
            i for i in _open_slot_indices(event_for_refresh) if i != self._slot_index
        ]

        if not remaining:
            if self._lock.locked():
                self._lock.release()
            await inter.response.send_message(
                texts.EVENT_FULL,
                ephemeral=True,
            )
            return

        refresh_view = SlotSelectionView(
            self._ctx, event_for_refresh, remaining, self._lock, self._user_id
        )
        await inter.response.send_message(
            texts.SLOT_TAKEN,
            view=refresh_view,
            ephemeral=True,
        )

    async def _handle_confirm(self, inter: disnake.MessageInteraction) -> None:
        """[✅ Confirm Booking] — full booking flow with PG lock + Notion write. (AC2–AC3, AC5–AC6)

        [Source: epics.md#Story 4.2 AC2, AC3]
        [Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
        [Source: architecture.md#Gap Analysis — PG lock vs Notion write boundary]
        """
        from bot.db.bookings import acquire_slot_lock, release_slot_lock
        from bot.notion.client import NotionWriteError, SlotRowMissingError
        from bot.services.dj_pages import ensure_dj_page
        from bot.ui.embeds import BookedEmbed

        # Guard: DB must be available (AC6)
        if self._ctx.db is None:
            if self._lock.locked():
                self._lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        event_id = self._event.page_id

        # Guard: Notion client must be available — without it we cannot write the booking (AC2)
        if self._ctx.notion_client is None:
            if self._lock.locked():
                self._lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        # Acquire exclusive slot lock via SELECT...FOR UPDATE (AC2)
        # AC3: Wrap in try/except — asyncpg auto-rolls back the transaction on exception
        try:
            slot_acquired = await acquire_slot_lock(
                self._ctx.db,
                event_id,
                self._slot_index,
                self._user_id,
            )
        except Exception:
            logger.exception(
                "BookingConfirmView: acquire_slot_lock raised — transaction rolled back"
            )
            if self._lock.locked():
                self._lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        if not slot_acquired:
            await self._present_slot_taken(inter)
            return

        # Get event name for embed + admin log
        event_name = self._event.event_id

        pg_released = False  # Postgres lock freed (never free it twice)
        responded = False  # an error response has gone out
        booked = False  # assign_slot succeeded: the booking exists in Notion

        async def _release_pg_lock() -> None:
            nonlocal pg_released
            if pg_released:
                return
            pg_released = True
            try:
                await release_slot_lock(
                    self._ctx.db, event_id, self._slot_index, expected_holder=self._user_id
                )
            except Exception:
                logger.exception("BookingConfirmView: release_slot_lock failed")

        def _release_user_lock() -> None:
            if self._lock.locked():
                self._lock.release()

        async def _abort_with_error() -> None:
            nonlocal responded
            await _release_pg_lock()
            _release_user_lock()
            if responded:
                return
            responded = True
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )

        # From here until the confirmation is sent, every exit must free both locks.
        try:
            # DJ's Notion page id (creates the DJs row if needed); never fatal to the flow
            try:
                dj_page_id = await ensure_dj_page(
                    self._ctx.db,
                    self._ctx.notion_client,
                    self._user_id,
                    inter.author.display_name,
                )
            except Exception:
                logger.exception("BookingConfirmView: ensure_dj_page raised")
                dj_page_id = None
            if dj_page_id is None:
                await _abort_with_error()
                return

            # Write to the Slots database; the row is re-read right before writing
            try:
                assigned = await self._ctx.notion_client.assign_slot(
                    self._event, self._slot_index, dj_page_id
                )
            except (NotionWriteError, SlotRowMissingError):
                logger.exception("BookingConfirmView: assign_slot failed")
                await _abort_with_error()
                return
            booked = assigned
            if not assigned:
                # Someone else already holds the Notion row. Read the Postgres holder
                # BEFORE freeing our lock so the conflict log names real DJs.
                try:
                    from bot.db.bookings import get_slot_lock_holder
                    pg_holder = await get_slot_lock_holder(
                        self._ctx.db, event_id, self._slot_index
                    )
                except Exception:
                    pg_holder = None
                notion_slot = next(
                    (x for x in self._event.slots if x.number == self._slot_index + 1), None
                )
                trigger = (
                    f"DJ {self._user_id} attempted slot {self._slot_index} in event "
                    f"{event_id!r}, but the Notion slot row already holds a DJ "
                    f"(Postgres lock holder: {pg_holder}; "
                    f"Notion slot DJ: {notion_slot.dj_name if notion_slot else None})"
                )
                await _release_pg_lock()
                await self._present_slot_taken(inter, trigger=trigger)
                return

            # Send booking confirmed embed (AC3)
            embed = BookedEmbed(self._event, self._slot_index)
            await inter.response.send_message(embed=embed)
        except asyncio.CancelledError:
            if not booked:
                await _release_pg_lock()
            _release_user_lock()
            raise
        except Exception:
            logger.exception("BookingConfirmView: unexpected error while saving booking")
            if booked:
                # The booking exists in Notion: keep the Postgres lock, no failure message
                _release_user_lock()
                return
            await _abort_with_error()
            return

        # Trigger lineup channel post — best-effort, failure must not block confirmation (AC3)
        if self._ctx.lineup_service is not None:
            try:
                self._ctx.lineup_service.notify_changed()
            except Exception:
                logger.exception("BookingConfirmView: lineup notify failed after booking")

        # Log to admin channel — best-effort (AC3)
        if self._ctx.log_service is not None:
            try:
                slot_display = f"<t:{self._slot_unix}:f>" if self._slot_unix else "TBD"
                await self._ctx.log_service.post_interaction(
                    dj_name=str(inter.author),
                    action_type="Booking",
                    outcome=f"Slot {self._slot_index + 1} confirmed",
                    event=f"{event_name} \u00b7 {slot_display}",
                )
            except Exception:
                logger.exception("BookingConfirmView: admin log post failed")

        # Release per-user lock — flow complete (AC3)
        if self._lock.locked():
            self._lock.release()


# ---------------------------------------------------------------------------
# Story 4.4: CancellationView
# ---------------------------------------------------------------------------

class CancellationView(disnake.ui.View):
    """One button per DJ's upcoming booked slot + [Never mind].

    Presented after DJ taps [❌ Cancel a Slot] from FallbackMenuView.
    The per-user lock is ALREADY HELD when this view is created.

    Buttons: one per booking (secondary style, max 4 on rows 0–3) +
             [Never mind] (secondary, row 4).
    timeout=None: DJ-facing views never expire (UX-DR16).

    [Source: epics.md#Story 4.4 AC1]
    [Source: ux-design-specification.md#UX-DR6, UX-DR13, UX-DR16]
    [Source: architecture.md#View Lifecycle Contract]
    """

    def __init__(
        self,
        ctx: "BotContext",
        bookings: list[dict],
        lock: asyncio.Lock,
        user_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self._ctx = ctx
        self._lock = lock
        self._user_id = user_id

        for i, booking in enumerate(bookings[:4]):  # max 4 booking buttons on rows 0-3
            event = booking["event"]
            label = f"{event.event_id} · {texts.slot_button(event, booking['slot_index'])}"[:80]

            btn = disnake.ui.Button(
                label=label,
                style=disnake.ButtonStyle.secondary,
                custom_id=f"cancel_select:{event.page_id}:{booking['slot_index']}",
                row=i,
            )

            async def _cb(
                inter: disnake.MessageInteraction,
                b: dict = booking,
            ) -> None:
                await self._handle_booking(inter, b)

            btn.callback = _cb
            self.add_item(btn)

        never_mind = disnake.ui.Button(
            label=texts.BTN_NEVER_MIND,
            style=disnake.ButtonStyle.secondary,
            custom_id="cancel_select:nevermind",
            row=4,
        )
        never_mind.callback = self._handle_never_mind
        self.add_item(never_mind)

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.
        [Source: architecture.md#View Lifecycle Contract]
        """

    async def _handle_never_mind(self, inter: disnake.MessageInteraction) -> None:
        """[Never mind] — release lock and respond warmly."""
        if self._lock.locked():
            self._lock.release()
        await inter.response.send_message(
            texts.CANCEL_KEPT,
            ephemeral=True,
        )

    async def _handle_booking(
        self,
        inter: disnake.MessageInteraction,
        booking: dict,
    ) -> None:
        """Booking selected — present CancelConfirmView. (AC3)

        [Source: epics.md#Story 4.4 AC3]
        """
        event = booking["event"]
        slot_index = booking["slot_index"]
        slot_unix = booking["slot_unix"]
        view = CancelConfirmView(
            ctx=self._ctx,
            event=event,
            slot_index=slot_index,
            slot_unix=slot_unix,
            lock=self._lock,
            user_id=self._user_id,
        )
        try:
            await inter.response.send_message(
                texts.cancel_confirm(event, slot_index),
                view=view,
                ephemeral=True,
            )
        except Exception:
            logger.exception("CancellationView: send_message failed")
            if self._lock.locked():
                self._lock.release()


# ---------------------------------------------------------------------------
# Story 4.4: CancelConfirmView
# ---------------------------------------------------------------------------

class CancelConfirmView(disnake.ui.View):
    """Two-button confirmation — confirms or keeps a pending slot cancellation.

    Presented alongside the "Are you sure?" message after DJ selects a booking
    in CancellationView. The per-user lock is ALREADY HELD when this view is created.

    Buttons:
      [✅ Yes, Cancel It] (danger)    — removes booking from Notion + frees slot lock
      [✖ Keep It]         (secondary) — releases lock, warm dismissal

    timeout=None: DJ-facing views never expire (UX-DR16).

    [Source: epics.md#Story 4.4 AC3–AC5]
    [Source: ux-design-specification.md#UX-DR7, UX-DR13, UX-DR16]
    [Source: architecture.md#View Lifecycle Contract]
    """

    def __init__(
        self,
        ctx: "BotContext",
        event: "EventInfo",
        slot_index: int,
        slot_unix: int,
        lock: asyncio.Lock,
        user_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self._ctx = ctx
        self._event = event
        self._slot_index = slot_index
        self._slot_unix = slot_unix
        self._lock = lock
        self._user_id = user_id

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.
        [Source: architecture.md#View Lifecycle Contract]
        """

    @disnake.ui.button(
        label=texts.BTN_CANCEL_YES,
        style=disnake.ButtonStyle.danger,
        custom_id="cancel_confirm:confirm",
    )
    async def confirm(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Remove booking from Notion, free slot lock, send confirmation. (AC4)"""
        # Story 6.1 AC3: log button tap
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        await self._handle_confirm(inter)

    @disnake.ui.button(
        label=texts.BTN_CANCEL_KEEP,
        style=disnake.ButtonStyle.secondary,
        custom_id="cancel_confirm:keep",
    )
    async def keep(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Keep booking — release lock, warm dismissal. (AC5)"""
        # Story 6.1 AC3: log button tap
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        await self._handle_keep(inter)

    async def _handle_keep(self, inter: disnake.MessageInteraction) -> None:
        """[✖ Keep It] — release per-user lock and respond warmly. (AC5)"""
        if self._lock.locked():
            self._lock.release()
        await inter.response.send_message(
            texts.CANCEL_KEPT,
            ephemeral=True,
        )

    async def _handle_confirm(self, inter: disnake.MessageInteraction) -> None:
        """[✅ Yes, Cancel It] — remove booking from Notion + free slot_locks row. (AC4)

        [Source: epics.md#Story 4.4 AC4]
        [Source: architecture.md#Slot Booking Atomicity]
        [Source: architecture.md#Gap Analysis — PG lock vs Notion write boundary]
        """
        from bot.db.bookings import release_slot_lock
        from bot.notion.client import NotionWriteError, SlotRowMissingError
        from bot.services.dj_pages import ensure_dj_page

        event_id = self._event.page_id

        # Guard: DB must be available
        if self._ctx.db is None:
            if self._lock.locked():
                self._lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        # Guard: Notion client must be available
        if self._ctx.notion_client is None:
            if self._lock.locked():
                self._lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return

        # Get event name for messages and admin log
        event_name = self._event.event_id

        # Remove from Notion — best-effort; slot lock MUST be freed regardless
        try:
            dj_page_id = await ensure_dj_page(
                self._ctx.db, self._ctx.notion_client, self._user_id, inter.author.display_name
            )
        except Exception:
            logger.exception("CancelConfirmView: ensure_dj_page raised")
            dj_page_id = None
        try:
            if dj_page_id is None:
                logger.warning(
                    "CancelConfirmView: no DJ page for user %s — Notion slot not cleared",
                    self._user_id,
                )
            elif not await self._ctx.notion_client.clear_slot(
                self._event, self._slot_index, dj_page_id
            ):
                logger.warning(
                    "CancelConfirmView: slot %s of %r is held by another DJ — Notion unchanged",
                    self._slot_index + 1,
                    event_name,
                )
        except SlotRowMissingError:
            logger.warning(
                "CancelConfirmView: slot row %s of %r is missing — Notion unchanged",
                self._slot_index + 1,
                event_name,
            )
        except NotionWriteError:
            logger.exception("CancelConfirmView: clear_slot failed")
            # Free the slot lock even on Notion failure — slot must become available
            await release_slot_lock(
                self._ctx.db, event_id, self._slot_index, expected_holder=self._user_id
            )
            if self._lock.locked():
                self._lock.release()
            await inter.response.send_message(
                texts.GENERIC_ERROR,
                ephemeral=True,
            )
            return
        except Exception:
            # Any other failure: keep going, the slot lock is freed below regardless
            logger.exception("CancelConfirmView: unexpected error clearing Notion slot")

        # Free the slot_locks DB row — slot is now open (AC4)
        await release_slot_lock(
            self._ctx.db, event_id, self._slot_index, expected_holder=self._user_id
        )

        # Confirmation message (AC4)
        await inter.response.send_message(
            texts.CANCELLED,
        )

        # Trigger lineup channel post — best-effort (AC4)
        if self._ctx.lineup_service is not None:
            try:
                self._ctx.lineup_service.notify_changed()
            except Exception:
                logger.exception("CancelConfirmView: lineup notify failed after cancellation")

        # Log to admin channel — best-effort
        if self._ctx.log_service is not None:
            try:
                slot_display = f"<t:{self._slot_unix}:f>" if self._slot_unix else "TBD"
                await self._ctx.log_service.post_interaction(
                    dj_name=str(inter.author),
                    action_type="Cancellation",
                    outcome=f"Slot {self._slot_index + 1} cancelled",
                    event=f"{event_name} \u00b7 {slot_display}",
                )
            except Exception:
                logger.exception("CancelConfirmView: admin log post failed")

        # Release per-user lock — flow complete (AC4)
        if self._lock.locked():
            self._lock.release()


# ---------------------------------------------------------------------------
# Story 5.2: OutreachView
# ---------------------------------------------------------------------------


class OutreachView(disnake.ui.View):
    """Proactive outreach — two buttons sent with a slot-fill invitation DM.

    Timeout: 43200 seconds (12 hours) — per UX-DR2, UX-DR16.
    On timeout: log ERR_BUTTON_TIMEOUT and mark contact as 'timeout'.

    Buttons:
      [🎧 Pick a Slot]          — success style — AC2
      [Not this time]           — secondary style — AC3

    [Source: epics.md#Story 5.2 AC2, AC3, AC5]
    [Source: ux-design-specification.md#UX-DR2, UX-DR16]
    [Source: architecture.md#View Lifecycle Contract]
    """

    def __init__(
        self,
        ctx: "BotContext",
        event: "EventInfo",
        discord_user_id: int,
    ) -> None:
        super().__init__(timeout=43200)  # 12 hours — UX-DR2
        self._ctx = ctx
        self._event = event
        self._discord_user_id = discord_user_id

    async def on_timeout(self) -> None:
        """Log ERR_BUTTON_TIMEOUT and mark contact as 'timeout' in DB.

        No message to DJ — timeout is silent from DJ perspective.

        [Source: epics.md#Story 5.2 AC5]
        [Source: architecture.md#Error Routing: Centralized AdminLogService]
        """
        from bot.db.outreach import update_outreach_contact_status
        from bot.ui.embeds import ErrorCode

        event_id = self._event.page_id
        if self._ctx.db is not None and event_id:
            try:
                await update_outreach_contact_status(
                    self._ctx.db, event_id, self._discord_user_id, "timeout"
                )
            except Exception as exc:
                logger.warning(
                    "OutreachView.on_timeout: failed to update contact status: %s", exc
                )

        if self._ctx.log_service is not None:
            event_name = self._event.event_id or event_id
            try:
                await self._ctx.log_service.post_error(
                    error_code=ErrorCode.ERR_BUTTON_TIMEOUT,
                    trigger=f"OutreachView timed out for DJ {self._discord_user_id} on event '{event_name}'",
                    action_taken="DJ marked as non-responsive for this sequence. Outreach will proceed to next eligible DJ after cooldown.",
                    manual_action="No action required unless the event date is imminent.",
                )
            except Exception:
                logger.exception("OutreachView.on_timeout: admin log post failed")

    @disnake.ui.button(
        label=texts.BTN_PICK_SLOT,
        style=disnake.ButtonStyle.success,
        custom_id="outreach:accept",
    )
    async def accept(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Enter slot selection booking flow for this event. (AC2)

        Acquires per-user lock FIRST — status is only updated to 'accepted' once
        the lock is held and the booking flow is actually starting. The view is
        stopped after lock acquisition to prevent re-use; decline remains available
        while the lock is held (another flow in progress).

        [Source: epics.md#Story 5.2 AC2]
        [Source: architecture.md#Per-User Flow Lock: asyncio.Lock per user_id]
        """
        # Story 6.1 AC3: log button tap
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        from bot.db.outreach import update_outreach_contact_status
        from bot.ui.embeds import LineupUpdateEmbed

        event_id = self._event.page_id

        # Acquire per-user lock FIRST — prevents false 'accepted' when another flow is open
        lock = self._ctx.get_user_lock(inter.author.id)
        if lock.locked():
            await inter.response.send_message(texts.FLOW_OPEN, ephemeral=True)
            return  # Don't stop view — DJ can still tap [Not this time]
        await lock.acquire()
        self.stop()  # Prevent re-use once booking flow is starting

        # Record acceptance AFTER lock acquired (accurate: DJ is entering booking flow)
        if self._ctx.db is not None and event_id:
            try:
                await update_outreach_contact_status(
                    self._ctx.db, event_id, self._discord_user_id, "accepted"
                )
            except Exception as exc:
                logger.warning("OutreachView.accept: DB update failed: %s", exc)

        # Fetch fresh event data; fall back to cached if Notion unavailable
        event = self._event
        if self._ctx.notion_client is not None:
            try:
                events = await self._ctx.notion_client.list_events()
                fresh = next((e for e in events if e.page_id == event_id), None)
                if fresh is not None:
                    event = fresh
            except Exception as exc:
                logger.warning("OutreachView.accept: list_events failed: %s", exc)

        # Compute open slot indices
        open_slot_indices = _open_slot_indices(event)

        if not open_slot_indices:
            lock.release()
            await inter.response.send_message(texts.INVITE_FILLED, ephemeral=True)
            return

        embed = LineupUpdateEmbed(event)
        slot_view = SlotSelectionView(self._ctx, event, open_slot_indices, lock, inter.author.id)
        try:
            await inter.response.send_message(
                content=_pick_slot_content(open_slot_indices),
                embed=embed,
                view=slot_view,
            )
        except Exception:
            logger.exception("OutreachView.accept: send_message failed")
            lock.release()

    @disnake.ui.button(
        label=texts.BTN_NOT_THIS_TIME,
        style=disnake.ButtonStyle.secondary,
        custom_id="outreach:decline",
    )
    async def decline(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Warm decline — mark as declined, no further action for this DJ. (AC3)

        The outreach sequence continues to the next eligible DJ after cooldown.

        [Source: epics.md#Story 5.2 AC3]
        [Source: ux-design-specification.md#UX-DR17 — warm, no urgency]
        """
        # Story 6.1 AC3: log button tap
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="DJ",
                content=f"[Button: {button.label}]",
            )
        self.stop()  # Prevent re-use after DJ responds
        from bot.db.outreach import update_outreach_contact_status

        event_id = self._event.page_id
        if self._ctx.db is not None and event_id:
            try:
                await update_outreach_contact_status(
                    self._ctx.db, event_id, self._discord_user_id, "declined"
                )
            except Exception as exc:
                logger.warning("OutreachView.decline: DB update failed: %s", exc)

        decline_text = texts.INVITE_DECLINED
        await inter.response.send_message(decline_text)
        # Story 6.1 AC1: log bot response
        if self._ctx.log_service is not None:
            await self._ctx.log_service.post_dm_transcript(
                dj_name=inter.author.name,
                direction="BOT",
                content=decline_text,
            )
