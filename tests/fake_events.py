"""Fake event reads for tests: seeds what bot.db.events.list_events / get_event return.

The autouse fixture in conftest.py patches the two repository functions to call
`fake_events.list_events`, an AsyncMock reset for every test, so a test sets
`fake_events.list_events = AsyncMock(return_value=[...])` (or a side_effect) to seed
events or to make the read fail, and asserts on it like on any AsyncMock.
"""
from unittest.mock import AsyncMock


class FakeEvents:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.list_events = AsyncMock(return_value=[])

    async def get_event(self, db: object, uid: str):
        events = await self.list_events(db)
        return next((e for e in events if e.page_id == uid), None)


fake_events = FakeEvents()
