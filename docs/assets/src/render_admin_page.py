"""Write admin page views with sample data to HTML files (for the README screenshots).

Run from the repository root:
    uv run python docs/assets/src/render_admin_page.py DASHBOARD.html SCHEDULE.html
"""
import datetime
import sys

from bot.db.outbox import OutboxStats
from bot.notion.models import EventInfo
from bot.services.schedule import ScheduleDate
from bot.services.schedule_rules import Rule
from bot.web.pages import render_dashboard, render_schedule
from bot.web.schedule_page import RuleView, ScheduleOverview
from bot.web.service import ActionResult, AdminRow, ServerInfo, SyncStatus, UserInfo

NOW = datetime.datetime(2026, 11, 28, 18, 4, tzinfo=datetime.UTC)

rows = [
    AdminRow(UserInfo(123456789012345678, "M4XXX", None, True), True),
    AdminRow(UserInfo(234567890123456789, "Luna", None, True), False),
    AdminRow(UserInfo(345678901234567890, None, None, False), False),
]
sync = SyncStatus(
    enabled=True,
    last_run=NOW,
    last_error=None,
    outbox=OutboxStats(count=0, oldest=None, last_error=None),
    issues=[],
    problem=None,
    maintenance=None,
)
dashboard = render_dashboard(
    ServerInfo(987654321098765432, "Rave Galaxy", None),
    rows,
    "preview",
    ActionResult(True, "Added Luna."),
    sync=sync,
)


def event(day: int, month: int, genre: str, location: str, by_hand: bool = False) -> EventInfo:
    date = datetime.date(2026, month, day)
    return EventInfo(
        page_id=f"ev-{date.isoformat()}",
        event_id=f"{date.isoformat()} Rave Night",
        date=date,
        status=None,
        genre=genre,
        notes="Doors open 19:30." if day == 5 else None,
        slot_count=6,
        start=datetime.time(20, 0),
        slot_minutes=60,
        slots=(),
        location=location,
        location_by_hand=by_hand,
    )


events = [
    event(5, 12, "Techno", "Club Vortex"),
    event(12, 12, "Techno", "Warehouse 9"),
    event(19, 12, "Techno", "Club Vortex"),
    event(26, 12, "Techno", "Rooftop Stage", by_hand=True),
]
dates = [ScheduleDate(e.date, e) for e in events]
second = datetime.date(2026, 12, 12)
overview = ScheduleOverview(
    enabled=True,
    interval_days=7,
    first_date=None,
    lookahead=4,
    defaults={"genre": "Techno", "location": "Club Vortex"},
    rules=[
        RuleView(
            Rule(1, "location", "Warehouse 9", 2, second),
            "Rave Night · 12.12.2026",
        )
    ],
    dates=dates,
    anchors=dates,
    pending=0,
)
schedule = render_schedule(
    overview,
    [(d.label, d.date.isoformat()) for d in dates],
    "preview",
    ActionResult(True, "The rule was added."),
)

for path, html in zip(sys.argv[1:3], (dashboard, schedule), strict=True):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
