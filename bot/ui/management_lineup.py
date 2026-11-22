"""Rendering of the DJ management lineup: channel embeds and DM payloads.

Pure functions, no Discord or database access. The channel message shows DJ
name and stream links only; Discord name, VRChat name and logo are DM-only.
"""
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import disnake

from bot.db import djs as db_djs
from bot.db import events as db_events
from bot.db.management_lineup import get_record_by_message
from bot.notion.models import STATUS_CANCELED, EventInfo, SlotInfo
from bot.services.fully_booked import is_fully_booked
from bot.ui import texts
from bot.ui.embeds import EmbedColors, _safe_dj_name
from bot.ui.gates import GatedView, _reply, refuse_in_maintenance

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)

LineupState = Literal["booked", "open", "canceled"]

_MAX_DESCRIPTION = 4000
_MAX_MESSAGE_CHARS = 5500  # Discord caps a message at 6000 characters of embed text
_MAX_EMBEDS = 10


def lineup_state(event: EventInfo) -> LineupState:
    if event.status == STATUS_CANCELED:
        return "canceled"
    if is_fully_booked(event):
        return "booked"
    return "open"


def _header_lines(event: EventInfo) -> list[str]:
    lines = [
        f"Start: {event.slot_start(0).strftime('%H:%M')}",
        f"Slot length: {event.slot_minutes} min",
    ]
    if event.genre:
        lines.append(f"Genre: {texts.escape_text(event.genre)}")
    if event.location:
        lines.append(f"Location: {texts.escape_text(event.location)}")
    note = (event.notes or "").strip()
    if note:
        lines.append(f"Notes: {texts.escape_text(note)}")
    return lines


def _slot_block(event: EventInfo, number: int, slot: SlotInfo | None, djs: dict[int, dict]) -> str:
    heading = texts.mgmt_slot_heading(number, event.slot_start(number - 1).strftime("%H:%M"))
    if slot is None or slot.dj_page_id is None:
        return f"{heading}\n{texts.MGMT_OPEN_SLOT}"
    details = djs.get(slot.discord_id) if slot.discord_id is not None else None
    details = details or {}
    name = _safe_dj_name(details.get("dj_name") or slot.dj_name)
    return "\n".join([
        heading,
        name,
        texts.mgmt_stream_line(details.get("stream_url_primary")),
        texts.mgmt_alt_stream_line(details.get("stream_url_backup")),
    ])


def build_channel_messages(
    event: EventInfo, djs: dict[int, dict], state: LineupState
) -> list[list[disnake.Embed]]:
    """One inner list per Discord message; each stays under the Discord limits."""
    title = texts.mgmt_title(event)
    head = _header_lines(event)
    if state == "canceled":
        body = "\n".join([*head, "", texts.MGMT_LINEUP_CANCELED])
        return [[disnake.Embed(title=title, description=body, color=EmbedColors.IMPERIAL_RED)]]
    if state == "open":
        head = [*head, "", texts.MGMT_LINEUP_OPEN_NOTE]
    color = EmbedColors.SUCCESS_GREEN if state == "booked" else EmbedColors.STARLIGHT_YELLOW

    by_number = event.resolved_slots()
    blocks = [
        _slot_block(event, n, by_number.get(n), djs) for n in range(1, event.slot_count + 1)
    ]

    messages: list[list[disnake.Embed]] = []
    # Each entry: [title, description lines]; embeds are built at the end.
    current: list[list] = [[title, ["\n".join(head)]]]
    used = len(title) + len(current[0][1][0])

    def flush() -> None:
        messages.append([
            disnake.Embed(title=t, description="\n\n".join(parts), color=color)
            for t, parts in current
        ])

    for block in blocks:
        embed = current[-1]
        desc_len = sum(len(p) for p in embed[1]) + 2 * len(embed[1])
        fits_embed = desc_len + len(block) + 2 <= _MAX_DESCRIPTION
        if fits_embed and used + len(block) + 2 <= _MAX_MESSAGE_CHARS:
            embed[1].append(block)
            used += len(block) + 2
            continue
        new_title = texts.MGMT_LINEUP_TITLE_CONTINUED
        if len(current) < _MAX_EMBEDS and used + len(new_title) + len(block) <= _MAX_MESSAGE_CHARS:
            current.append([new_title, [block]])
            used += len(new_title) + len(block)
            continue
        flush()
        current = [[new_title, [block]]]
        used = len(new_title) + len(block)
    flush()
    return messages


def content_hash(messages: list[list[disnake.Embed]]) -> str:
    payload = json.dumps(
        [[e.to_dict() for e in msg] for msg in messages], sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class DmPayload:
    text: str
    logo: bytes | None
    logo_filename: str


def _logo_filename(logo: bytes | None, number: int) -> str:
    ext = "png"
    if logo:
        if logo.startswith(b"\xff\xd8"):
            ext = "jpg"
        elif logo.startswith(b"GIF8"):
            ext = "gif"
        elif logo.startswith(b"RIFF") and logo[8:12] == b"WEBP":
            ext = "webp"
    return f"logo-slot-{number}.{ext}"


def build_dm_payloads(
    event: EventInfo,
    djs: dict[int, dict],
    discord_names: dict[int, str],
    logos: dict[int, bytes],
) -> list[DmPayload]:
    """Header first, then one payload per booked slot in slot order."""
    header = "\n".join([texts.mgmt_title(event), *_header_lines(event)])
    payloads = [DmPayload(text=header, logo=None, logo_filename="")]
    by_number = event.resolved_slots()
    for n in range(1, event.slot_count + 1):
        slot = by_number.get(n)
        if slot is None or slot.dj_page_id is None:
            continue
        uid = slot.discord_id
        details = (djs.get(uid) if uid is not None else None) or {}
        discord_name = discord_names.get(uid) if uid is not None and uid > 0 else None
        logo = logos.get(uid) if uid is not None else None
        lines = [
            texts.mgmt_slot_heading(n, event.slot_start(n - 1).strftime("%H:%M")),
            _safe_dj_name(details.get("dj_name") or slot.dj_name),
            f"Discord: {texts.escape_text(discord_name) if discord_name else texts.MGMT_NONE}",
            f"VRChat: {details.get('vrchat_name') or texts.MGMT_NONE}",
            texts.mgmt_stream_line(details.get("stream_url_primary")),
            texts.mgmt_alt_stream_line(details.get("stream_url_backup")),
        ]
        if logo is None:
            lines.append(f"Logo: {details.get('logo_url') or texts.MGMT_NONE}")
        payloads.append(
            DmPayload(text="\n".join(lines), logo=logo, logo_filename=_logo_filename(logo, n))
        )
    return payloads


async def _discord_name(bot, uid: int) -> str | None:
    """Discord name of a user; None for guests (ids <= 0) and on any failure."""
    if uid <= 0:
        return None
    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
    except Exception:
        return None
    if user is None:
        return None
    return getattr(user, "display_name", None) or getattr(user, "name", None)


class ManagementInfoView(GatedView):
    """Persistent "Send me more information" button; admins only."""

    def __init__(self, ctx: "BotContext") -> None:
        super().__init__(timeout=None)
        self.ctx = ctx

    @disnake.ui.button(
        label="Send me more information",
        style=disnake.ButtonStyle.secondary,
        custom_id="mgmt:info",
    )
    async def send_info(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        if await refuse_in_maintenance(inter, self.ctx):
            return
        # Privacy: nothing about the lineup is loaded before this check passes
        if not self.ctx.is_admin(inter.author.id):
            await _reply(inter, texts.MGMT_INFO_ADMINS_ONLY)
            return
        db = self.ctx.db
        try:
            record = await get_record_by_message(db, inter.message.id)
        except Exception:
            logger.exception("Lineup info: record lookup failed")
            await _reply(inter, texts.MGMT_INFO_FAILED)
            return
        if record is None:
            await _reply(inter, texts.MGMT_INFO_UNTRACKED)
            return
        await inter.response.defer(ephemeral=True)
        try:
            event = await db_events.get_event(db, record.event_id)
            if event is None:
                await inter.followup.send(texts.MGMT_INFO_UNTRACKED, ephemeral=True)
                return
            uids = [
                s.discord_id for s in event.slots
                if s.dj_page_id is not None and s.discord_id is not None
            ]
            djs = await db_djs.get_dj_details(db, uids)
            names: dict[int, str] = {}
            logos: dict[int, bytes] = {}
            for uid in uids:
                name = await _discord_name(inter.bot, uid)  # None for guests
                if name:
                    names[uid] = name
                logo = await db_djs.get_logo_image(db, uid)
                if logo:
                    logos[uid] = logo
            payloads = build_dm_payloads(event, djs, names, logos)
        except Exception:
            logger.exception("Lineup info: loading the lineup failed")
            await inter.followup.send(texts.MGMT_INFO_FAILED, ephemeral=True)
            return
        sent = 0  # DMs sent, the header included
        for payload in payloads:
            try:
                if payload.logo is not None:
                    file = disnake.File(io.BytesIO(payload.logo), filename=payload.logo_filename)
                    await inter.author.send(payload.text, file=file)
                else:
                    await inter.author.send(payload.text)
            except disnake.Forbidden:
                if sent == 0:
                    await inter.followup.send(texts.MGMT_INFO_DMS_CLOSED, ephemeral=True)
                else:
                    await inter.followup.send(texts.mgmt_info_partial(sent - 1), ephemeral=True)
                return
            except Exception:
                logger.exception("Lineup info: a DM failed")
                await inter.followup.send(texts.mgmt_info_partial(max(sent - 1, 0)), ephemeral=True)
                return
            sent += 1
        await inter.followup.send(texts.MGMT_INFO_SENT, ephemeral=True)
