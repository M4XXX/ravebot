"""Write the admin page dashboard with sample data to an HTML file (for the README screenshot).

Run from the repository root: uv run python docs/assets/src/render_admin_page.py OUT.html
"""
import sys

from bot.web.pages import render_dashboard
from bot.web.service import ActionResult, AdminRow, ServerInfo, UserInfo

rows = [
    AdminRow(UserInfo(123456789012345678, "M4XXX", None, True), True),
    AdminRow(UserInfo(234567890123456789, "Luna", None, True), False),
    AdminRow(UserInfo(345678901234567890, None, None, False), False),
]
html = render_dashboard(
    ServerInfo(987654321098765432, "Rave Galaxy", None), rows, "preview", ActionResult(True, "Added Luna.")
)
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(html)
