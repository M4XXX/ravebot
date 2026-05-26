"""Outreach service — gap detection and sequence lifecycle management.

Handles: FR25–30 — DJ queue logic, cooldown checks, contact sequencing.
Story 5.1 scope: gap detection, sequence upsert/complete, cooldown gating.
Story 5.2 scope: DJ selection and DM delivery (extend this service).

[Source: architecture.md#Scheduled Work: disnake tasks.loop]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
[Source: architecture.md#Cog Responsibility Boundary]
"""
import datetime
import logging
from typing import TYPE_CHECKING, Any

import disnake

from bot.db.config import get_value
from bot.db.djs import get_active_booking_djs
from bot.db.djs import update_last_contacted_at as update_dj_last_contacted_at
from bot.db.interventions import is_under_intervention
from bot.db.outreach import (
    get_contacted_discord_ids,
    get_sequence,
    mark_sequence_complete,
    mark_sequence_exhausted,
    reactivate_sequence,
    record_outreach_contact,
    update_last_contact_at,
    update_outreach_contact_status,
    upsert_sequence,
)

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)

_DEFAULT_OUTREACH_DAYS = 14
_DEFAULT_COOLDOWN_HOURS = 12


class OutreachService:
    """Stateless outreach logic — gap detection, sequence lifecycle, cooldown enforcement.

    Constructor mirrors LineupService: bot, db, notion_client, log_service.
    All per-event state is stored in the outreach_sequences table — never in instance vars.

    [Source: bot/services/lineup.py — constructor pattern]
    [Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
    """

    def __init__(
        self,
        bot: Any,
        db: Any,
        notion_client: Any,
        log_service: "AdminLogService",
    ) -> None:
        self.bot = bot
        self.db = db
        self.notion_client = notion_client
        self.log_service = log_service

    async def run_gap_detection(self, guild_id: int) -> list[dict]:
        """Detect events with open slots within the outreach window.

        Returns a list of Notion event dicts that are ready for outreach:
          - event is within the outreach window (now <= event_date <= now + outreach_days)
          - event has at least one open slot (slots_total - djs_booked > 0)
          - outreach sequence is active (status == 'active')
          - cooldown has elapsed (last_contact_at is NULL or > cooldown_hours ago)

        Story 5.2 iterates this list to select DJs and send outreach DMs.

        Open slot formula is identical to _filter_events_with_open_slots() in views.py
        to ensure consistent slot counting across the codebase.

        [Source: epics.md#Story 5.1 AC1–AC4]
        [Source: bot/ui/views.py:728 — _filter_events_with_open_slots formula]
        [Source: architecture.md#Cog Responsibility Boundary]
        """
        # Read config from DB with integer defaults
        try:
            days_raw = await get_value(self.db, guild_id, "outreach_days_before")
            outreach_days = int(days_raw) if days_raw is not None else _DEFAULT_OUTREACH_DAYS
        except (ValueError, TypeError):
            outreach_days = _DEFAULT_OUTREACH_DAYS

        try:
            hours_raw = await get_value(self.db, guild_id, "outreach_cooldown_hours")
            cooldown_hours = int(hours_raw) if hours_raw is not None else _DEFAULT_COOLDOWN_HOURS
        except (ValueError, TypeError):
            cooldown_hours = _DEFAULT_COOLDOWN_HOURS

        # Fetch all future events from Notion (AC1)
        try:
            events = await self.notion_client.poll_events()
        except Exception as exc:
            logger.warning("OutreachService.run_gap_detection: poll_events failed: %s", exc)
            return []

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        window_end = now_utc + datetime.timedelta(days=outreach_days)
        ready: list[dict] = []

        for event in events:
            event_id = event.get("id", "")
            if not event_id:
                continue

            # Parse event date — skip if missing or outside window (AC1)
            try:
                date_str = event["properties"]["Date"]["date"]["start"]
                event_dt = datetime.datetime.fromisoformat(date_str)
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=datetime.timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue

            if not (now_utc <= event_dt <= window_end):
                continue

            # Compute open slots — mirrors _filter_events_with_open_slots in views.py
            # [Source: bot/ui/views.py:728–749]
            try:
                raw_slots = event["properties"]["Slots"]["number"]
                slots_total = max(1, int(raw_slots) if raw_slots is not None else 6)
            except (KeyError, TypeError, ValueError):
                slots_total = 6
            try:
                djs_booked = len(event["properties"]["DJs"]["relation"])
            except (KeyError, TypeError):
                djs_booked = 0

            open_slots = slots_total - djs_booked

            # AC4: all slots filled — mark active sequence complete
            if open_slots <= 0:
                try:
                    seq = await get_sequence(self.db, event_id)
                    if seq is not None and seq["status"] == "active":
                        await mark_sequence_complete(self.db, event_id)
                        logger.info(
                            "OutreachService: event %s fully booked — sequence marked complete",
                            event_id,
                        )
                except Exception as exc:
                    logger.warning(
                        "OutreachService: error marking sequence complete for %s: %s",
                        event_id,
                        exc,
                    )
                continue

            # AC2: open slots — ensure sequence row exists (idempotent)
            try:
                await upsert_sequence(self.db, event_id)
                seq = await get_sequence(self.db, event_id)
            except Exception as exc:
                logger.warning(
                    "OutreachService: DB error for event %s: %s", event_id, exc
                )
                continue

            if seq is None:
                # Should not happen after upsert; guard defensively
                continue

            # Skip complete sequences — no open slots, nothing to do
            if seq["status"] == "complete":
                continue

            # AC3: for exhausted sequences, check if new DJs joined since exhaustion
            if seq["status"] == "exhausted":
                try:
                    all_active = await get_active_booking_djs(self.db)
                    already_contacted = set(
                        await get_contacted_discord_ids(self.db, event_id)
                    )
                    new_eligible = [
                        dj
                        for dj in all_active
                        if dj["discord_user_id"] not in already_contacted
                    ]
                except Exception as exc:
                    logger.warning(
                        "OutreachService.run_gap_detection: AC3 check failed for %s: %s",
                        event_id,
                        exc,
                    )
                    continue

                if not new_eligible:
                    continue  # No new DJs — stay exhausted

                # New DJs available — reactivate and fall through to cooldown check
                try:
                    await reactivate_sequence(self.db, event_id)
                    seq = await get_sequence(self.db, event_id)
                    if seq is None:
                        continue
                except Exception as exc:
                    logger.warning(
                        "OutreachService.run_gap_detection: failed to reactivate %s: %s",
                        event_id,
                        exc,
                    )
                    continue
                logger.info(
                    "OutreachService.run_gap_detection: exhausted sequence %s reactivated"
                    " — %d new eligible DJ(s)",
                    event_id,
                    len(new_eligible),
                )
                # Fall through to cooldown check with refreshed seq

            # AC3: cooldown check — skip if last_contact_at is within cooldown window
            last_contact = seq.get("last_contact_at")
            if last_contact is not None:
                if last_contact.tzinfo is None:
                    last_contact = last_contact.replace(tzinfo=datetime.timezone.utc)
                elapsed = now_utc - last_contact
                if elapsed < datetime.timedelta(hours=cooldown_hours):
                    continue  # within cooldown — skip this cycle

            # Event is ready: new sequence (last_contact_at NULL) or past cooldown
            ready.append(event)

        logger.info(
            "OutreachService.run_gap_detection: %d event(s) ready for outreach"
            " (window=%dd, cooldown=%dh)",
            len(ready),
            outreach_days,
            cooldown_hours,
        )
        return ready

    async def contact_next_dj(self, event: dict, guild_id: int, ctx: "Any") -> bool:
        """Select next eligible DJ and send proactive outreach DM.

        Returns True if a DJ was selected (regardless of DM success/failure).
        Returns False if no eligible DJs remain (all active DJs contacted in
        this sequence) — caller should check exhaustion for Story 5.3.

        DJ selection: active_booking=true, not yet in outreach_contacts for
        this event, ordered by last_contacted_at ASC NULLS FIRST (cross-sequence
        fairness — DJs never contacted anywhere get priority).

        ctx: BotContext — passed from OutreachCog for user lock access and notion_client.

        [Source: epics.md#Story 5.2 AC1–AC5]
        [Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
        """
        from bot.ui.embeds import ProactiveOutreachEmbed
        from bot.ui.views import OutreachView

        event_id = event.get("id", "")
        if not event_id:
            return False

        # Fetch active DJs (ordered by last_contacted_at ASC NULLS FIRST)
        try:
            all_active = await get_active_booking_djs(self.db)
            already_contacted = set(await get_contacted_discord_ids(self.db, event_id))
        except Exception as exc:
            logger.warning("OutreachService.contact_next_dj: DB error: %s", exc)
            return False

        eligible = [dj for dj in all_active if dj["discord_user_id"] not in already_contacted]
        if not eligible:
            logger.info(
                "OutreachService.contact_next_dj: no eligible DJs for event %s — all contacted",
                event_id,
            )
            return False

        dj = eligible[0]
        discord_user_id = dj["discord_user_id"]
        dj_name = dj.get("dj_name", str(discord_user_id))

        # Story 6.2 AC6: skip DJs under admin intervention silently
        try:
            if await is_under_intervention(self.db, discord_user_id):
                logger.info(
                    "OutreachService.contact_next_dj: skipping DJ %s (%s) — under admin intervention",
                    dj_name,
                    discord_user_id,
                )
                return True
        except Exception as exc:
            logger.warning(
                "OutreachService.contact_next_dj: intervention check failed for %s: %s",
                discord_user_id,
                exc,
            )

        # Record contact attempt BEFORE sending (idempotent if this cycle retries)
        try:
            await record_outreach_contact(self.db, event_id, discord_user_id)
        except Exception as exc:
            logger.warning(
                "OutreachService.contact_next_dj: failed to record contact for %s: %s",
                discord_user_id,
                exc,
            )
            return False

        # Get Discord User object
        user = self.bot.get_user(discord_user_id)
        if user is None:
            logger.warning(
                "OutreachService.contact_next_dj: user %s not found in cache — marking unreachable",
                discord_user_id,
            )
            await self._mark_unreachable(
                event_id, discord_user_id, dj_name, reason="User not in bot cache"
            )
            return True

        # Get event name for opener text
        try:
            event_name = event["properties"]["Name"]["title"][0]["plain_text"]
        except (KeyError, IndexError):
            event_name = "an upcoming event"

        # Build embed and view
        embed = ProactiveOutreachEmbed(event)
        view = OutreachView(ctx=ctx, event=event, discord_user_id=discord_user_id)
        opener = (
            f"Hey! There's an open slot at **{event_name}** — "
            "want to jump in? Take a look:"
        )

        # Send DM
        try:
            await user.send(content=opener, embed=embed, view=view)
            # Story 6.1 AC1: log outgoing outreach DM
            if self.log_service is not None:
                await self.log_service.post_dm_transcript(
                    dj_name=dj_name,
                    direction="BOT",
                    content=opener,
                )
        except disnake.Forbidden:
            await self._mark_unreachable(
                event_id, discord_user_id, dj_name, reason="DMs blocked"
            )
            return True
        except Exception as exc:
            logger.warning(
                "OutreachService.contact_next_dj: DM send failed for %s: %s",
                discord_user_id,
                exc,
            )
            return True

        # DM sent — update timestamps
        try:
            await update_last_contact_at(self.db, event_id)
            await update_dj_last_contacted_at(self.db, discord_user_id)
        except Exception as exc:
            logger.warning(
                "OutreachService.contact_next_dj: timestamp update failed: %s", exc
            )

        logger.info(
            "OutreachService.contact_next_dj: DM sent to DJ %s (%s) for event %s",
            dj_name,
            discord_user_id,
            event_id,
        )
        return True

    async def handle_exhaustion(self, event: dict, guild_id: int) -> None:  # noqa: ARG002
        """Check outreach exhaustion and post ERR_OUTREACH_EXHAUSTED if warranted.

        guild_id is accepted for API consistency with other service methods and
        future config-driven behaviour (per-guild outreach settings).

        Called by OutreachCog._outreach_check() when contact_next_dj() returns False
        (no eligible DJs remain for this event's sequence).

        Guards against duplicate alerts: only acts when sequence status == 'active'.
        After marking exhausted, run_gap_detection() will skip this sequence on
        subsequent cycles unless a new DJ opts in (AC3 reactivation in run_gap_detection).

        [Source: epics.md#Story 5.3 AC1, AC2]
        [Source: architecture.md#Error Routing: Centralized AdminLogService]
        [Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
        """
        from bot.ui.embeds import ErrorCode

        event_id = event.get("id", "")
        if not event_id:
            return

        # AC2: only proceed if sequence is still 'active' — prevents duplicate alert
        try:
            seq = await get_sequence(self.db, event_id)
        except Exception as exc:
            logger.warning(
                "OutreachService.handle_exhaustion: DB error fetching seq: %s", exc
            )
            return

        if seq is None or seq["status"] != "active":
            return  # Already exhausted, complete, or no sequence

        # Compute open slots (same formula as run_gap_detection)
        try:
            raw_slots = event["properties"]["Slots"]["number"]
            slots_total = max(1, int(raw_slots) if raw_slots is not None else 6)
        except (KeyError, TypeError, ValueError):
            slots_total = 6
        try:
            djs_booked = len(event["properties"]["DJs"]["relation"])
        except (KeyError, TypeError):
            djs_booked = 0
        open_slots = slots_total - djs_booked

        if open_slots <= 0:
            # Race: slots filled between contact_next_dj call and here — no alert needed
            return

        # Count DJs contacted in this sequence for the alert message
        try:
            contacted_ids = await get_contacted_discord_ids(self.db, event_id)
            djs_contacted_count = len(contacted_ids)
        except Exception as exc:
            logger.warning(
                "OutreachService.handle_exhaustion: DB error counting contacts: %s", exc
            )
            djs_contacted_count = 0

        # Mark sequence exhausted BEFORE alerting (prevents race on rapid task cycles)
        try:
            await mark_sequence_exhausted(self.db, event_id)
        except Exception as exc:
            logger.warning(
                "OutreachService.handle_exhaustion: failed to mark exhausted: %s", exc
            )
            return

        # Build alert context from event
        try:
            event_name = event["properties"]["Name"]["title"][0]["plain_text"]
        except (KeyError, IndexError):
            event_name = event_id
        try:
            date_str = event["properties"]["Date"]["date"]["start"]
            unix_ts = int(
                datetime.datetime.fromisoformat(date_str).timestamp()
            )
            date_display = f"<t:{unix_ts}:f>"
        except (KeyError, TypeError, ValueError):
            date_display = "TBD"

        # Post ERR_OUTREACH_EXHAUSTED to admin log (AC1)
        if self.log_service is not None:
            try:
                await self.log_service.post_error(
                    error_code=ErrorCode.ERR_OUTREACH_EXHAUSTED,
                    trigger=(
                        f"All Active Booking DJs contacted for '{event_name}'"
                        f" ({date_display}). Open slots remaining: {open_slots}."
                        f" DJs contacted: {djs_contacted_count}."
                    ),
                    action_taken=(
                        "Outreach sequence marked exhausted. No further automated"
                        " outreach for this event."
                    ),
                    manual_action=(
                        "Manually contact DJs or add new Active Booking DJs to the pool. "
                        "New DJs opting in will trigger a fresh outreach pass automatically."
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "OutreachService.handle_exhaustion: post_error failed: %s", exc
                )

        logger.info(
            "OutreachService.handle_exhaustion: event %s exhausted — "
            "%d open slot(s), %d DJ(s) contacted",
            event_id,
            open_slots,
            djs_contacted_count,
        )

    async def _mark_unreachable(
        self, event_id: str, discord_user_id: int, dj_name: str, reason: str
    ) -> None:
        """Mark a DJ as unreachable and post ERR_DM_FAILED to admin log."""
        from bot.ui.embeds import ErrorCode

        try:
            await update_outreach_contact_status(
                self.db, event_id, discord_user_id, "unreachable"
            )
        except Exception as exc:
            logger.warning("_mark_unreachable: DB update failed: %s", exc)

        if self.log_service is not None:
            await self.log_service.post_error(
                error_code=ErrorCode.ERR_DM_FAILED,
                trigger=(
                    f"Outreach DM failed for DJ '{dj_name}' (ID: {discord_user_id})"
                    f" on event '{event_id}': {reason}"
                ),
                action_taken="DJ marked as unreachable for this sequence. Outreach will proceed to next eligible DJ.",
                manual_action="Verify the DJ's Discord privacy settings or their presence in the server.",
            )
