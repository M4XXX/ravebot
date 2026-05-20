"""discord.ui.View subclasses — implemented across Story 2.x–5.x.

Views: OnboardingIntroView (Story 2.1), OnboardingModal + RetryOnboardingModalView (Story 2.2),
       OutreachView, SlotSelectionView, BookingConfirmView, CancellationView,
       CancelConfirmView, FallbackMenuView.

RULE: Every View subclass MUST:
  1. Accept BotContext as a constructor argument
  2. Implement on_timeout() — re-present flow or send warm timeout message
  3. Acquire user's asyncio.Lock on entry; release on completion, abandonment, and in on_timeout()
"""
import asyncio
import logging
from typing import TYPE_CHECKING

import disnake

from bot.db.djs import insert_dj, update_active_booking
from bot.ui.embeds import ErrorCode

if TYPE_CHECKING:
    from bot.context import BotContext

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
        label="✅ Go Active",
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
        lock = self.ctx.get_user_lock(inter.author.id)
        if lock.locked():
            await inter.response.send_message(
                "Looks like you've got a flow already open — let's finish that one first.",
                ephemeral=True,
            )
            return
        await lock.acquire()
        await inter.response.send_modal(
            OnboardingModal(self.ctx, inter.author.id, lock, self.guild_id)
        )

    @disnake.ui.button(
        label="Pass for now",
        style=disnake.ButtonStyle.secondary,
        custom_id="onboarding:pass_for_now",
    )
    async def pass_for_now(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Warm decline — no DB write, no role assignment (Story 2.4 AC1)."""
        await inter.response.send_message(
            "No problem — you can always join later if you change your mind."
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
                    placeholder="Your VRChat username (optional — skip if not applicable)",
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
                    placeholder="https://twitch.tv/yourname (optional — skip if not applicable)",
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
            field_list = " and ".join(invalid_fields)
            plural = "don't" if len(invalid_fields) > 1 else "doesn't"
            await inter.response.send_message(
                f"Hmm, {field_list} {plural} look right — "
                "URLs should start with https://. "
                "Tap the button below to try again.",
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
                "Something went wrong on our end — please try again in a moment.",
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

        # Story 2.3: Notion DJ record sync (best-effort — failure does not block confirmation)
        if self._ctx.notion_client is not None:
            try:
                await self._ctx.notion_client.write_dj_record(
                    discord_user_id=inter.author.id,
                    dj_name=dj_name,
                )
            except Exception:
                logger.exception(
                    "Unexpected error during Notion DJ record sync for user %s",
                    inter.author.id,
                )

        await inter.response.send_message(
            "You're all set! I'll reach out when there's an open slot, "
            "or you can start a booking anytime.",
        )


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
        label="Try again",
        style=disnake.ButtonStyle.primary,
        custom_id="onboarding:retry_modal",
    )
    async def retry(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Re-open the onboarding modal. Re-acquires the per-user lock."""
        lock = self._ctx.get_user_lock(inter.author.id)
        if lock.locked():
            await inter.response.send_message(
                "Looks like you've got a flow already open — let's finish that one first.",
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

    Buttons per UX-DR1:
      Row 0: [Book a Slot] (primary) · [Cancel a Slot] (secondary)
      Row 1: [Go Active] (success) · [Go Inactive] (secondary)
      Row 2: [Show Lineup] (primary)

    timeout=None: DJ-facing views never expire (UX-DR16).
    No per-user lock: Go Active/Go Inactive are single-step interactions.

    [Source: epics.md#Story 2.4 AC2-AC4]
    [Source: ux-design-specification.md#UX-DR1, UX-DR13, UX-DR16]
    """

    def __init__(self, ctx: "BotContext") -> None:
        super().__init__(timeout=None)
        self.ctx = ctx

    async def on_timeout(self) -> None:
        """No-op: timeout=None means this never fires.
        [Source: architecture.md#View Lifecycle Contract]
        """

    @disnake.ui.button(
        label="📅 Book a Slot",
        style=disnake.ButtonStyle.primary,
        custom_id="fallback:book_slot",
        row=0,
    )
    async def book_slot(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Booking flow — implemented in Epic 4 (Story 4.1)."""
        await inter.response.send_message(
            "Booking is coming soon — stay tuned!",
            ephemeral=True,
        )

    @disnake.ui.button(
        label="❌ Cancel a Slot",
        style=disnake.ButtonStyle.secondary,
        custom_id="fallback:cancel_slot",
        row=0,
    )
    async def cancel_slot(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Cancellation flow — implemented in Epic 4 (Story 4.4)."""
        await inter.response.send_message(
            "Slot cancellation is coming soon — stay tuned!",
            ephemeral=True,
        )

    @disnake.ui.button(
        label="✅ Go Active",
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
        if self.ctx.db is None:
            await inter.response.send_message(
                "Something went wrong on our end — please try again in a moment.",
                ephemeral=True,
            )
            return

        updated = await update_active_booking(
            self.ctx.db,
            discord_user_id=inter.author.id,
            active_booking=True,
        )

        if not updated:
            await inter.response.send_message(
                "Looks like you haven't set up your profile yet! "
                "You'll need to complete onboarding first — "
                "ask an organizer to re-assign your DJ role to get started.",
            )
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

        await inter.response.send_message(
            "You're back in the pool — I'll reach out when there's an open slot."
        )

    @disnake.ui.button(
        label="🚫 Go Inactive",
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
        if self.ctx.db is None:
            await inter.response.send_message(
                "Something went wrong on our end — please try again in a moment.",
                ephemeral=True,
            )
            return

        await update_active_booking(
            self.ctx.db,
            discord_user_id=inter.author.id,
            active_booking=False,
        )

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

        await inter.response.send_message(
            "Got it — I'll stop reaching out. You can still self-book anytime."
        )

    @disnake.ui.button(
        label="🌌 Show Lineup",
        style=disnake.ButtonStyle.primary,
        custom_id="fallback:show_lineup",
        row=2,
    )
    async def show_lineup(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        """Lineup view — implemented in Epic 3 (Story 3.3)."""
        await inter.response.send_message(
            "Lineup display is coming soon — stay tuned!",
            ephemeral=True,
        )
