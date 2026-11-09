"""Critical log entries in plain words: assert on the embeds a log channel receives."""
import datetime
import re
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import disnake
import httpx
import pytest

from bot.log import AdminLogService
from bot.notion.api import NotionApi
from bot.services.lineup import LineupService
from bot.services.notion_sync import SyncAction
from bot.services.outreach import OutreachService
from bot.services.reminders import RemindersService
from tests.notion.factories import make_event, make_slot

DM_TODO = (
    "Tell {name} yourself. They need to allow DMs from server members "
    "(Discord Settings, Privacy & Safety)."
)
NOTION_TODO = "Check the Slots rows in Notion. The next sync retries."
DJ_CHANNEL_TODO = "Run /setup again to set the DJ channel."
SECRET_WORDS = re.compile(r"ERR_|Error|Exception|Forbidden|HTTP \d|Traceback|user_id|ID:")


class Recorder(AdminLogService):
    """Real AdminLogService whose fake channel keeps every embed it receives."""

    def __init__(self) -> None:
        super().__init__(MagicMock(), MagicMock())
        self.embeds: list[disnake.Embed] = []

    async def _send(self, embed, view=None):  # noqa: ANN001
        self.embeds.append(embed)
        return None

    @property
    def only(self) -> disnake.Embed:
        assert len(self.embeds) == 1, [e.title for e in self.embeds]
        return self.embeds[0]


def todo_of(embed: disnake.Embed) -> str | None:
    return embed.fields[0].value if embed.fields else None


def forbidden() -> disnake.Forbidden:
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    return disnake.Forbidden(resp, "Cannot send messages to this user")


EVENT = make_event(
    event_id="Rave Night",
    page_id="evt-001",
    date=datetime.date(2026, 5, 22),
    start=datetime.time(0, 0),
)


async def welcome_blocked(log: Recorder) -> None:
    from bot.cogs.onboarding import OnboardingCog
    from tests.cogs.test_onboarding import make_ctx, make_member, make_role

    cog = OnboardingCog(make_ctx(db=MagicMock(), log=log))
    before = make_member()
    before.roles = []
    after = make_member(user_id=987654321, name="anna_login")
    after.display_name = "DJ Anna"
    after.roles = [make_role("DJ")]
    after.send = AsyncMock(side_effect=forbidden())
    with patch("bot.cogs.onboarding.get_dj_by_discord_id", new=AsyncMock(return_value=None)):
        await cog.on_member_update(before, after)


async def reminder_blocked(log: Recorder) -> None:
    svc = RemindersService(
        bot=MagicMock(),
        db=MagicMock(),
        notion_client=MagicMock(list_events=AsyncMock(return_value=[EVENT])),
        log_service=log,
    )
    user = MagicMock(send=AsyncMock(side_effect=forbidden()))
    svc.bot.get_user = MagicMock(return_value=user)
    booked = {"discord_user_id": 123456789, "dj_name": "DJ Anna", "slot_index": 1,
              "event_id": "evt-001"}
    now = datetime.datetime(2026, 5, 21, 2, 0, tzinfo=datetime.UTC)
    with patch("bot.services.reminders.get_booked_djs_for_event",
               new=AsyncMock(return_value=[booked])), \
         patch("bot.services.reminders.has_reminder_been_sent", new=AsyncMock(return_value=False)), \
         patch("bot.services.reminders.record_reminder_sent", new=AsyncMock()):
        await svc.send_reminders(guild_id=1, ctx=MagicMock(), _now=now)


async def invite_blocked(log: Recorder) -> None:
    svc = OutreachService(
        bot=MagicMock(), db=MagicMock(), notion_client=MagicMock(), log_service=log
    )
    user = MagicMock(send=AsyncMock(side_effect=forbidden()))
    svc.bot.get_user = MagicMock(return_value=user)
    dj = {"discord_user_id": 123456789, "dj_name": "DJ Anna"}
    with patch("bot.services.outreach.get_active_booking_djs", new=AsyncMock(return_value=[dj])), \
         patch("bot.services.outreach.get_contacted_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.get_waiting_discord_ids", new=AsyncMock(return_value=[])), \
         patch("bot.services.outreach.record_outreach_contact", new=AsyncMock()), \
         patch("bot.services.outreach.update_outreach_contact_status", new=AsyncMock()):
        await svc.contact_next_dj(EVENT, guild_id=1, ctx=MagicMock())


async def book_slot_blocked(log: Recorder) -> None:
    from bot.ui.views import LineupPostView

    ctx = MagicMock(log_service=log)
    view = LineupPostView(ctx)
    inter = MagicMock()
    inter.author.name = "anna_login"
    inter.author.display_name = "DJ Anna"
    inter.author.id = 123456789
    inter.author.send = AsyncMock(side_effect=forbidden())
    inter.followup.send = AsyncMock()
    await view._dm_or_report(inter, "hello", None, release_lock=False)


async def db_unavailable_events_none(log: Recorder) -> None:
    from bot.cogs.events import EventsCog

    await EventsCog(MagicMock(db=None, log_service=log))._guard_db("notion_poll")


async def db_unavailable_events_down(log: Recorder) -> None:
    from bot.cogs.events import EventsCog

    db = MagicMock(fetchval=AsyncMock(side_effect=asyncpg.PostgresConnectionError("db down")))
    await EventsCog(MagicMock(db=db, log_service=log))._guard_db("notion_poll")


async def db_unavailable_outreach_none(log: Recorder) -> None:
    from bot.cogs.outreach_cog import OutreachCog

    await OutreachCog(MagicMock(db=None, log_service=log))._guard_db("outreach_check")


async def db_unavailable_outreach_down(log: Recorder) -> None:
    from bot.cogs.outreach_cog import OutreachCog

    db = MagicMock(fetchval=AsyncMock(side_effect=OSError("refused")))
    await OutreachCog(MagicMock(db=db, log_service=log))._guard_db("outreach_check")


async def outreach_exhausted(log: Recorder, contacted: tuple[int, ...] = (101, 102)) -> None:
    slots = [make_slot(number=i + 1, dj_page_id=f"dj-{i}", dj_name=f"DJ {i}") for i in range(2)]
    event = make_event(event_id="Rave Night", page_id="evt-001",
                       date=datetime.date(2030, 5, 22), slot_count=6, slots=slots)
    svc = OutreachService(
        bot=MagicMock(), db=MagicMock(), notion_client=MagicMock(), log_service=log
    )
    seq = {"event_id": "evt-001", "status": "active", "last_contact_at": None}
    with patch("bot.services.outreach.get_sequence", new=AsyncMock(return_value=seq)), \
         patch("bot.services.outreach.get_contacted_discord_ids",
               new=AsyncMock(return_value=list(contacted))), \
         patch("bot.services.outreach.mark_sequence_exhausted", new=AsyncMock()):
        await svc.handle_exhaustion(event, guild_id=1)


async def notion_write_failed(log: Recorder) -> None:
    api = NotionApi("secret", log, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(Exception):  # noqa: B017, PT011
            await api.request("PATCH", "/pages/abc", {}, "assign slot 2 of Rave Night")


async def notion_read_failed(log: Recorder) -> None:
    api = NotionApi("secret", log, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(Exception):  # noqa: B017, PT011
            await api.request("POST", "/data_sources/x/query", {}, "query data source x")


async def sync_cycle_failed(log: Recorder) -> None:
    from bot.cogs.events import EventsCog

    sync = MagicMock(run_cycle=AsyncMock(side_effect=RuntimeError("boom 123")))
    cog = EventsCog(MagicMock(notion_sync_service=sync, log_service=log))
    await cog._run_sync_cycle(1)


async def poll_read_failed(log: Recorder) -> None:
    from bot.cogs.events import EventsCog

    ctx = MagicMock(log_service=log, notion_sync_service=None)
    ctx.db.fetchval = AsyncMock(return_value=1)
    ctx.notion_client.list_events = AsyncMock(side_effect=httpx.RequestError("timeout"))
    cog = EventsCog(ctx)
    cog.bot = MagicMock(guilds=[MagicMock(id=1)])
    from bot.cogs.events import EventsCog as C

    await C._poll_notion.coro(cog)


async def sync_write_failed(log: Recorder) -> None:
    from bot.services.notion_sync import NotionSyncService

    svc = NotionSyncService(MagicMock(), MagicMock(), MagicMock(), log)
    action = SyncAction(kind="free", event=EVENT, slot_index=1, discord_id=5, detail="x")
    await svc._notion_failed(action, RuntimeError("kaputt"))


async def no_dj_channel(log: Recorder) -> None:
    svc = LineupService(bot=MagicMock(guilds=[MagicMock(id=1)]), db=MagicMock(),
                        log_service=log, notion_client=MagicMock())
    with patch("bot.services.lineup.get_value", new=AsyncMock(return_value=None)):
        await svc._send(1, EVENT)


BLOCKED = ("Could not DM a DJ", DM_TODO.format(name="DJ Anna"))
CASES = [
    pytest.param(welcome_blocked, *BLOCKED, ["DJ Anna"], id="welcome-dm"),
    pytest.param(reminder_blocked, *BLOCKED, ["DJ Anna", "Rave Night", "slot 2 at 01:00"],
                 id="reminder-dm"),
    pytest.param(invite_blocked, *BLOCKED, ["DJ Anna", "Rave Night"], id="invite-dm"),
    pytest.param(book_slot_blocked, *BLOCKED, ["DJ Anna"], id="book-a-slot-dm"),
    pytest.param(db_unavailable_events_none, "Database unavailable", None, [], id="db-events-none"),
    pytest.param(db_unavailable_events_down, "Database unavailable", None, [], id="db-events-down"),
    pytest.param(db_unavailable_outreach_none, "Database unavailable", None, [], id="db-out-none"),
    pytest.param(db_unavailable_outreach_down, "Database unavailable", None, [], id="db-out-down"),
    pytest.param(outreach_exhausted, "No more DJs to ask",
                 "Ask DJs yourself or add new DJs to the pool. A new DJ joining the pool "
                 "starts another round of invites.", ["Rave Night", "4 slots"], id="exhausted"),
    pytest.param(notion_write_failed, "Notion write failed", NOTION_TODO, [],
                 id="notion-write"),
    pytest.param(sync_write_failed, "Notion write failed", NOTION_TODO,
                 ["Rave Night"], id="sync-write"),
    pytest.param(notion_read_failed, "Something failed", None, ["read from Notion"],
                 id="notion-read"),
    pytest.param(sync_cycle_failed, "Something failed", None, ["Notion"], id="sync-cycle"),
    pytest.param(poll_read_failed, "Something failed", None, ["Notion"], id="poll-read"),
    pytest.param(no_dj_channel, "Something failed", DJ_CHANNEL_TODO, ["lineup"],
                 id="no-dj-channel"),
]


@pytest.mark.parametrize(("drive", "title", "todo", "mentions"), CASES)
async def test_entry_wording(drive, title, todo, mentions) -> None:  # noqa: ANN001
    log = Recorder()
    await drive(log)
    embed = log.only
    assert embed.title == title
    if todo is not None:
        assert todo_of(embed) == todo
    elif title != "Something failed":
        assert embed.fields == []
    text = f"{embed.title} {embed.description} {todo_of(embed) or ''}"
    for needle in mentions:
        assert needle in text, (needle, text)
    assert not SECRET_WORDS.search(text), text
    assert not re.search(r"\b\d{9,}\b", text), text  # no raw ids
    assert "—" not in text
    assert "boom" not in text and "kaputt" not in text and "timeout" not in text


async def test_exhausted_names_slots_and_not_a_zero() -> None:
    log = Recorder()
    await outreach_exhausted(log)
    assert "4 slots are still open" in log.only.description


async def test_exhausted_singular_and_plural_wording() -> None:
    log = Recorder()
    await outreach_exhausted(log, contacted=(101,))
    assert "The only DJ in the pool was asked" in log.only.description
    assert "All 1 DJs" not in log.only.description
    log = Recorder()
    await outreach_exhausted(log)
    assert "All 2 DJs in the pool were asked" in log.only.description


async def test_slot_conflict_and_timeout_are_not_posted() -> None:
    from bot.ui.embeds import ErrorCode

    log = Recorder()
    await log.post_error(ErrorCode.ERR_SLOT_CONFLICT, "x", "y")
    await log.post_error(ErrorCode.ERR_BUTTON_TIMEOUT, "x", "y")
    assert log.embeds == []


async def test_post_error_escapes_trigger_and_action() -> None:
    from bot.ui.embeds import ErrorCode

    log = Recorder()
    await log.post_error(
        ErrorCode.ERR_DM_FAILED, "@everyone <@123> **bold**", "_under_ @here", "todo"
    )
    desc = log.only.description
    assert "@everyone" not in desc.replace("@​everyone", "")
    assert "<@123>" not in desc and "**bold**" not in desc and "@here" not in desc.replace(
        "@​here", ""
    )
