"""DJ booking and cancellation end to end: real SQL, real Notion client, fake Notion server.

Opt-in like test_postgres.py (RAVEBOT_TEST_POSTGRES_URL), same fixtures and guard (conftest.py).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.pg import SKIP_WITHOUT_URL

pytestmark = [pytest.mark.pg, pytest.mark.real_slots, SKIP_WITHOUT_URL]

EVENT = {"id": "evt-1", "properties": {
    "Name": {"title": [{"plain_text": "Rave"}]},
    "Date": {"date": {"start": "2026-11-07"}},
    "DJs": {"relation": []},
}}


class Lineup:
    """The event page's DJs relation as Notion would keep it."""

    def __init__(self) -> None:
        self.relation: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"properties": {"DJs": {
                "id": "djs", "has_more": False,
                "relation": [{"id": pid} for pid in self.relation]}}})
        if request.method == "PATCH":
            body = json.loads(request.content)
            self.relation = [r["id"] for r in body["properties"]["DJs"]["relation"]]
            return httpx.Response(200, json={"id": "evt-1"})
        return httpx.Response(404, json={})


def _ctx(pool, lineup: Lineup):
    from bot.context import BotContext
    from bot.notion.client import NotionClient, _refuse_deletion

    client = NotionClient(api_key="secret_test", database_id="db", log_service=AsyncMock())
    client._client = httpx.AsyncClient(
        base_url="https://api.notion.com/v1",
        transport=httpx.MockTransport(lineup.handler),
        event_hooks={"request": [_refuse_deletion]},
    )
    client.poll_events = AsyncMock(return_value=[EVENT])
    return BotContext(config=MagicMock(), db=pool, notion_client=client)


def _inter(uid: int) -> MagicMock:
    inter = MagicMock()
    inter.author.id = uid
    inter.response.send_message = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    return inter


async def _dj(pool, uid: int, page: str) -> None:
    from bot.db.djs import insert_dj, set_notion_dj_page_id

    await insert_dj(pool, discord_user_id=uid, dj_name=f"dj{uid}", vrchat_name=None,
                    logo_url=None, stream_url_primary="https://twitch.tv/x", stream_url_backup=None)
    await set_notion_dj_page_id(pool, uid, page)


async def _book(ctx, uid: int, slot: int) -> None:
    from bot.ui.views import BookingConfirmView

    lock = ctx.get_user_lock(uid)
    await lock.acquire()
    await BookingConfirmView(ctx, EVENT, slot, 0, lock, uid)._handle_confirm(_inter(uid))


async def _cancel_view(ctx, uid: int, slot: int):
    from bot.ui.views import CancelConfirmView

    lock = ctx.get_user_lock(uid)
    await lock.acquire()
    return CancelConfirmView(ctx, EVENT, slot, 0, lock, uid)


async def test_two_slots_one_cancel_and_a_stale_button(pool) -> None:
    from bot.db.bookings import get_booked_slots

    lineup = Lineup()
    ctx = _ctx(pool, lineup)
    await _dj(pool, 111, "page-A")
    await _dj(pool, 222, "page-B")

    await _book(ctx, 111, 0)
    await _book(ctx, 111, 1)
    assert lineup.relation == ["page-A"]

    first = await _cancel_view(ctx, 111, 0)
    await first._handle_confirm(_inter(111))
    assert await get_booked_slots(pool, ["evt-1"]) == {"evt-1": {1: 111}}
    assert lineup.relation == ["page-A"]  # still plays slot 2

    await _book(ctx, 222, 0)
    again = _inter(111)
    await first._handle_confirm(again)  # the old cancel button, tapped a second time
    assert await get_booked_slots(pool, ["evt-1"]) == {"evt-1": {0: 222, 1: 111}}
    assert "no longer booked by you" in again.response.send_message.call_args.args[0]

    last = await _cancel_view(ctx, 111, 1)
    await last._handle_confirm(_inter(111))
    assert lineup.relation == ["page-B"]
