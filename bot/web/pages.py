"""Server rendered HTML for the admin page (space theme, no JavaScript, inline CSS)."""

from __future__ import annotations

import datetime
import json
from html import escape
from urllib.parse import quote

from bot.db.announcements import AnnouncementRule
from bot.db.issues import IssueRow
from bot.policy import BookingRules
from bot.services.announcement_template import (
    DEFAULT_TEMPLATES,
    GENERAL,
    MAX_LENGTH,
    PLACEHOLDERS,
    START,
    TemplateCheck,
)
from bot.ui import texts
from bot.web.schedule_page import PendingView, RuleView, ScheduleOverview, pick_key
from bot.web.service import (
    ActionResult,
    AdminRow,
    AnnouncementPageData,
    ServerInfo,
    SyncStatus,
)

_CDN = "https://cdn.discordapp.com/"
_BRAND = "Rave<span>Bot</span>&nbsp;Admin&nbsp;Page"
_AVATAR_VARIANTS = ("", " b", " c")

CSS = """
*{box-sizing:border-box}
html{min-height:100%;background:#05060f}
body{margin:0;min-height:100vh;color:#e8eaff;font:14px/1.45 system-ui,-apple-system,"Segoe UI",
Roboto,sans-serif;background:radial-gradient(ellipse at 20% 0%,#1b1f4b 0%,#0b0d24 55%,#05060f 100%)
fixed}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:
radial-gradient(1.5px 1.5px at 12% 20%,#fff8 50%,transparent 51%),
radial-gradient(1px 1px at 70% 35%,#fff9 50%,transparent 51%),
radial-gradient(1.5px 1.5px at 85% 12%,#fff7 50%,transparent 51%),
radial-gradient(1px 1px at 40% 80%,#fff6 50%,transparent 51%),
radial-gradient(1px 1px at 92% 70%,#fff8 50%,transparent 51%),
radial-gradient(1.5px 1.5px at 25% 60%,#fff6 50%,transparent 51%)}
.space{position:relative;max-width:720px;margin:0 auto;padding:22px 16px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-size:20px;font-weight:700;letter-spacing:.5px;line-height:1.6;padding-bottom:4px;
overflow:visible;white-space:nowrap}
.brand span{color:#9d8cff}
.card{background:#ffffff0d;border:1px solid #ffffff22;border-radius:10px;padding:14px 16px;
margin:12px 0;backdrop-filter:blur(2px)}
.card h3{margin:0 0 8px;font-size:14px;color:#b9c0ff;text-transform:uppercase;letter-spacing:1px}
.row{display:flex;align-items:center;flex-wrap:wrap;gap:12px;padding:8px 0;
border-top:1px solid #ffffff14}
.row:first-of-type{border-top:none}
.av{width:38px;height:38px;border-radius:50%;flex:none;object-fit:cover;
background:linear-gradient(135deg,#7c5cff,#2ad4ff)}
.av.b{background:linear-gradient(135deg,#ff7ca8,#ffb86b)}
.av.c{background:linear-gradient(135deg,#4cd9a0,#2a8cff)}
.who{min-width:0;overflow-wrap:anywhere}
.who b{display:block}
.who small{opacity:.65}
.tag.g{border-color:#ffb86b99;color:#ffd9a8}
.tag{margin-left:auto;font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid #9d8cff88;
color:#cfc6ff;white-space:nowrap}
.btn{display:inline-block;padding:6px 14px;border:0;border-radius:6px;background:#7c5cff;
color:#fff;font:inherit;font-size:12.5px;text-decoration:none;cursor:pointer}
.btn.red{background:#c2415a}
.btn.g{background:#ffffff1f}
.btn:focus-visible,.in:focus-visible{outline:2px solid #2ad4ff;outline-offset:2px}
.in{display:inline-block;width:230px;max-width:100%;padding:6px 10px;border:1px solid #ffffff33;
border-radius:6px;background:#00000040;color:#e8eaff;font:inherit;margin:0 8px 8px 0}
.in::placeholder{color:#e8eaff;opacity:.6}
.srv{display:flex;align-items:center;gap:12px}
.planet{width:42px;height:42px;border-radius:50%;flex:none;object-fit:cover;
background:radial-gradient(circle at 30% 30%,#ffd89b,#c2693a 60%,#5a2a1a)}
.login{max-width:340px;margin:30px auto;text-align:center}
.login .brand,.login p,.login .in,.login .btn{display:block;text-align:center;margin-left:auto;
margin-right:auto}
.login .in{width:200px;margin-bottom:16px}
.login .btn{width:90px}
.login .planet{width:64px;height:64px;margin:0 auto 12px}
.muted{opacity:.65;font-size:12.5px}
.msg{padding:8px 12px;border-radius:8px;margin:12px 0;border:1px solid}
.msg.ok{background:#4cd9a01f;border-color:#4cd9a066}
.msg.err{background:#c2415a26;border-color:#c2415a88}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.kv{margin:4px 0;padding-left:18px;overflow-wrap:anywhere}
.fld{display:inline-flex;flex-direction:column;width:230px;max-width:100%;margin:0 8px 12px 0;
vertical-align:top}
.fld>span{font-size:12px;font-weight:600;color:#b9c0ff;margin-bottom:4px}
.fld .in{width:100%;margin:0}
.fld small{opacity:.6;font-size:11.5px;margin-top:4px;line-height:1.35}
.sub{margin-top:16px;padding:14px 14px 4px;border:1px solid #9d8cff44;border-radius:8px;
background:#9d8cff0f}
.sub h4{margin:0 0 4px;font-size:13px;color:#e8eaff}
.sub>p{margin:0 0 12px}
form{margin:0}
.ta{display:block;width:100%;margin:0 0 8px;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace}
.pv{white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 12px;padding:10px 12px;border-radius:6px;
background:#00000040;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace}
.ph{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:8px 0 12px;
font-size:12.5px}
.ph dt{font-family:ui-monospace,Menlo,Consolas,monospace;color:#cfc6ff}
.ph dd{margin:0;opacity:.8}
"""


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _safe_url(url: str | None) -> str | None:
    return url if url and url.startswith(_CDN) else None


def _page(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>RaveBot Admin Page</title><style>{CSS}</style></head>"
        f'<body><main class="space">{body}</main></body></html>'
    )


def _csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(csrf)}">'


def _message(message: ActionResult | None) -> str:
    if message is None:
        return ""
    kind = "ok" if message.ok else "err"
    return f'<p class="msg {kind}" role="status">{_e(message.message)}</p>'


def _image(url: str | None, css_class: str) -> str:
    safe = _safe_url(url)
    if safe is None:
        return f'<div class="{css_class}"></div>'
    return (
        f'<img class="{css_class}" src="{_e(safe)}" alt="" width="38" height="38" '
        'referrerpolicy="no-referrer">'
    )


def _admin_row(row: AdminRow, index: int, with_remove: bool = True) -> str:
    info = row.info
    name = info.name or "Unknown user"
    detail = _e(info.user_id) + ("" if info.on_server else " &middot; not on the server")
    if row.is_owner:
        tail = '<span class="tag">Owner (environment key)</span>'
    else:
        tail = '<span class="tag">Added here</span>'
        if with_remove:
            tail += f'<a class="btn red" href="/admins/remove?id={_e(info.user_id)}">Remove</a>'

    variant = _AVATAR_VARIANTS[index % len(_AVATAR_VARIANTS)]
    return (
        f'<div class="row">{_image(info.avatar_url, "av" + variant)}'
        f'<div class="who"><b>{_e(name)}</b><small>{detail}</small></div>{tail}</div>'
    )


def render_login(error: str | None = None) -> str:
    error_html = f'<p class="msg err" role="alert">{_e(error)}</p>' if error else ""
    return _page(
        '<div class="login card"><div class="planet"></div>'
        f'<div class="brand">{_BRAND}</div>'
        '<p class="muted">Enter the admin password</p>'
        f"{error_html}"
        '<form method="post" action="/login">'
        '<input class="in" type="password" name="password" aria-label="Admin password" '
        'autocomplete="current-password" required autofocus>'
        '<button class="btn" type="submit">Launch</button></form></div>'
    )


def _notion_card(problem: str | None, paused: bool = False) -> str:
    if not problem:
        return ""
    effect = (
        "The one-time import from Notion has not run, so booking and edits are paused. "
        "If the integration only lost access, share the Notion page with it again and "
        "retry the import (Notion sync below) or restart the bot."
        if paused
        else "Booking and the lineup keep working; changes reach Notion again once the "
        "databases are back. If the integration only lost access, share the Notion page "
        "with it again and restart the bot."
    )
    return (
        '<section class="card"><h3>Notion databases not found</h3>'
        f'<p class="msg err">{_e(problem)}</p>'
        f"<p>{effect} If the databases were deleted on purpose, create a new set.</p>"
        '<a class="btn red" href="/notion/recreate">Create new databases</a></section>'
    )


def render_confirm_recreate(problem: str, csrf: str, import_pending: bool = False) -> str:
    if import_pending:
        warning = (
            '<p class="msg err"><b>The one-time import from Notion has not run yet.</b> '
            "Creating the new, empty databases marks the one-time import from Notion as "
            "done for good with zero events: the bot never imports the old databases "
            "later. It then starts with no events and no bookings, creates new events on "
            "its own and starts DJ outreach for them.</p>"
            "<p>If the integration only lost access to the old databases, share the "
            "Notion page with it again and retry the import instead.</p>"
        )
    else:
        warning = (
            "<p>If the one-time import from Notion after the update has not run yet, "
            "creating the new, empty databases marks the one-time import from Notion as "
            "done for good: the bot never imports the old databases later.</p>"
        )
    return _page(
        f"{_TOP}"
        '<section class="card"><h3>Create new Notion databases</h3>'
        f'<p class="muted">{_e(problem)}</p>'
        "<p>The bot creates a new set of Events, Slots, DJs and Bot data databases on the "
        "Notion page. Nothing is copied: bookings and DJs in the old databases stay there, "
        "and the bookings the bot remembers point to the old events.</p>"
        f"{warning}"
        '<div class="actions"><form method="post" action="/notion/recreate">'
        f'{_csrf_field(csrf)}<button class="btn red" type="submit">Create new databases'
        '</button></form><a class="btn g" href="/">Cancel</a></div></section>'
    )


def _time(value: datetime.datetime | None) -> str:
    if value is None:
        return "never"
    return value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _data_card(sync: SyncStatus | None) -> str:
    busy = sync.maintenance if sync is not None else None
    busy_html = f'<p class="msg err">{_e(busy)}</p>' if busy else ""
    return (
        '<section class="card"><h3>Data and backup</h3>'
        f"{busy_html}"
        '<p class="muted">The backup file holds the whole database: events, slots, DJs with '
        "logos, settings and the admins added here.</p>"
        '<div class="actions"><a class="btn" href="/backup/export">Download a backup</a>'
        '<a class="btn red" href="/backup/import">Restore a backup</a></div></section>'
    )


_ISSUE_LABELS = {
    "postgres": "Keep Postgres",
    "notion": "Keep Notion",
    "resynced": "Resync Postgres to Notion",
}


def _issue_choices(issue: IssueRow) -> tuple[str, ...]:
    return ("postgres", "notion") if issue.kind == "conflict" else ("resynced",)


def _issue_row(issue: IssueRow) -> str:
    kind = "Conflict" if issue.kind == "conflict" else "Warning"
    where = " ".join(str(x) for x in (issue.entity, issue.entity_key) if x)
    detail = f"{_e(where)} &middot; " if where else ""
    buttons = "".join(
        f'<a class="btn g" href="/sync/issues/{_e(issue.id)}?choice={choice}">'
        f"{_ISSUE_LABELS[choice]}</a>"
        for choice in _issue_choices(issue)
    )
    return (
        f'<div class="row"><div class="who"><b>{_e(issue.summary)}</b>'
        f"<small>{detail}since {_time(issue.created_at)}</small></div>"
        f'<span class="tag g">{kind}</span>{buttons}</div>'
    )


def _retry_block(problem: str) -> str:
    return (
        f'<p class="msg err">{_e(problem)}</p>'
        '<p>Booking and the Notion sync are paused until the import from Notion '
        "works. Fix the data in Notion, then retry, or rebuild from Notion.</p>"
        '<a class="btn red" href="/sync/retry-import">Retry the import from Notion</a>'
    )


def _sync_card(sync: SyncStatus | None) -> str:
    if sync is None:
        return ""
    if not sync.enabled:
        if not (sync.can_retry and sync.problem):
            return ""
        # Notion is configured but its databases could not be reached at startup.
        return (
            '<section class="card"><h3>Notion sync</h3>'
            f"{_retry_block(sync.problem)}</section>"
        )
    problem = _retry_block(sync.problem) if sync.problem else ""
    box = sync.outbox
    waiting = f"{box.count} waiting"
    if box.oldest is not None:
        waiting += f", oldest since {_time(box.oldest)}"
    errors = ""
    if sync.last_error:
        errors += f'<p class="msg err">Last sync error: {_e(sync.last_error)}</p>'
    if box.last_error:
        errors += f'<p class="msg err">Last Notion write error: {_e(box.last_error)}</p>'
    conflicts = [i for i in sync.issues if i.kind == "conflict"]
    warnings = [i for i in sync.issues if i.kind != "conflict"]
    issue_html = "".join(_issue_row(i) for i in conflicts + warnings)
    if not issue_html:
        issue_html = '<p class="muted">No open conflicts or warnings.</p>'
    return (
        '<section class="card"><h3>Notion sync</h3>'
        f"{problem}"
        f"<p>Last sync: {_time(sync.last_run)}<br>Changes for Notion: {_e(waiting)}</p>"
        f"{errors}"
        f"<p><b>Conflicts ({len(conflicts)}) and warnings ({len(warnings)})</b></p>"
        f"{issue_html}"
        '<div class="actions"><a class="btn g" href="/sync/resync">Resync all to Notion</a>'
        '<a class="btn red" href="/sync/rebuild">Rebuild from Notion</a></div></section>'
    )


def render_dashboard(
    server: ServerInfo | None,
    rows: list[AdminRow],
    csrf: str,
    message: ActionResult | None = None,
    notion_problem: str | None = None,
    sync: SyncStatus | None = None,
    notion_paused: bool = False,
) -> str:
    if server is None:
        server_html = '<p class="muted">The bot is not on a server yet.</p>'
    else:
        server_html = (
            f'<div class="srv">{_image(server.icon_url, "planet")}'
            f'<div class="who"><b>{_e(server.name)}</b><small>Server ID {_e(server.guild_id)} '
            "&middot; the server the bot works on</small></div></div>"
        )
    admin_rows = "".join(_admin_row(row, i) for i, row in enumerate(rows))
    if not rows:
        admin_rows = '<p class="muted">No admins yet.</p>'
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div>'
        f'<form method="post" action="/logout">{_csrf_field(csrf)}'
        '<button class="btn g" type="submit">Log out</button></form></div>'
        f"{_message(message)}"
        f"{_notion_card(notion_problem, notion_paused)}"
        f'<section class="card"><h3>Server</h3>{server_html}</section>'
        '<section class="card"><h3>Add an admin</h3>'
        f'<form method="post" action="/admins/add">{_csrf_field(csrf)}'
        '<input class="in" type="text" name="id" inputmode="numeric" maxlength="25" '
        'placeholder="Discord user ID" aria-label="Discord user ID" autocomplete="off" required>'
        '<button class="btn" type="submit">Add</button></form>'
        '<p class="muted">After adding, the person can use the admin commands right away. '
        "Name and picture are shown when the person is on the server.</p></section>"
        f'<section class="card"><h3>Admins with bot commands ({len(rows)})</h3>'
        f"{admin_rows}</section>"
        '<section class="card"><h3>DJs</h3>'
        '<p class="muted">Add, edit or delete DJs, including guests without a Discord account.</p>'
        '<a class="btn" href="/djs">Manage DJs</a></section>'
        '<section class="card"><h3>Schedule</h3>'
        '<p class="muted">Genre and location defaults, rules and the next events.</p>'
        '<a class="btn" href="/schedule">Schedule</a></section>'
        '<section class="card"><h3>Booking rules</h3>'
        '<p class="muted">Slot limit per DJ and event, and the cancellation cutoff.</p>'
        '<a class="btn" href="/booking-rules">Booking rules</a></section>'
        '<section class="card"><h3>Announcements</h3>'
        '<p class="muted">Event announcements: channel, switches, rules and templates.</p>'
        '<a class="btn" href="/announcements">Announcements</a></section>'
        f"{_data_card(sync)}"
        f"{_sync_card(sync)}"
    )


def _confirm(title: str, body: str, action: str, button: str, csrf: str, extra: str = "") -> str:
    return _page(
        f"{_TOP}"
        f'<section class="card"><h3>{_e(title)}</h3>{body}'
        f'<div class="actions"><form method="post" action="{_e(action)}">'
        f'{_csrf_field(csrf)}{extra}<button class="btn red" type="submit">{_e(button)}'
        '</button></form><a class="btn g" href="/">Cancel</a></div></section>'
    )


def render_backup_import(csrf: str) -> str:
    return _page(
        f"{_TOP}"
        '<section class="card"><h3>Restore a backup</h3>'
        "<p>The backup replaces the whole database in one step: events, slots, DJs, "
        "settings and the admins added here. Changes made since the backup are lost.</p>"
        '<p class="muted">The bot first saves a safety backup of the current data. Bookings '
        "and edits are refused while the restore runs. A file from a newer bot version is "
        "refused. With Notion set up, everything is written to Notion again afterwards.</p>"
        '<form method="post" action="/backup/import" enctype="multipart/form-data">'
        f"{_csrf_field(csrf)}"
        '<input class="in" type="file" name="backup" accept=".gz,application/gzip" '
        'aria-label="Backup file" required>'
        '<div class="actions"><button class="btn red" type="submit">Replace the database'
        '</button><a class="btn g" href="/">Cancel</a></div></form></section>'
    )


def render_confirm_rebuild(csrf: str) -> str:
    body = (
        "<p>The bot saves a safety backup, then replaces events, slots, DJs with logos, "
        "settings and the bot data with what Notion holds.</p>"
        '<p class="muted">Bookings and edits are refused while it runs. Changes still '
        "waiting for Notion and open conflicts are dropped. Admins added here are not in "
        "Notion: the owners from ADMIN_USER_IDS keep working, the others come back only "
        "from a backup file.</p>"
    )
    return _confirm("Rebuild from Notion", body, "/sync/rebuild", "Rebuild from Notion", csrf)


def render_confirm_resync(csrf: str) -> str:
    body = (
        "<p>Every event, DJ and slot is written to Notion again.</p>"
        '<p class="muted">Hand edits in Notion that the bot has not taken over are '
        "overwritten with the bot's data.</p>"
    )
    return _confirm("Resync all to Notion", body, "/sync/resync", "Resync all", csrf)


def render_confirm_retry_import(problem: str, csrf: str) -> str:
    body = (
        f'<p class="msg err">{_e(problem)}</p>'
        "<p>The bot saves a safety backup and runs the import from Notion again. When it "
        "works, booking and the Notion sync start again.</p>"
        '<p class="muted">Fix the problems above in Notion first, or the import fails '
        "again.</p>"
    )
    return _confirm(
        "Retry the import from Notion", body, "/sync/retry-import", "Retry the import", csrf
    )


_CHOICE_TEXT = {
    "postgres": "The bot's version is kept and written to Notion. The Notion edit is "
    "overwritten.",
    "notion": "Notion's version is applied to the bot after the same checks as a command. "
    "If the checks fail, the conflict stays open.",
    "resynced": "The bot's version is written to Notion again. The Notion edit is "
    "overwritten.",
}


def _payload(title: str, payload: dict | None) -> str:
    if not payload:
        return ""
    items = "".join(
        f"<li>{_e(key)}: {_e(json.dumps(value, ensure_ascii=False, default=str))}</li>"
        for key, value in sorted(payload.items(), key=lambda kv: str(kv[0]))
    )
    return f"<p><b>{_e(title)}</b></p><ul class=\"kv\">{items}</ul>"


def render_confirm_issue(issue: IssueRow, choice: str, csrf: str) -> str:
    where = " ".join(str(x) for x in (issue.entity, issue.entity_key) if x)
    body = (
        f"<p><b>{_e(issue.summary)}</b></p>"
        f'<p class="muted">{_e(where)} &middot; since {_time(issue.created_at)}</p>'
        f'{_payload("In the bot", issue.pg_payload)}'
        f'{_payload("In Notion", issue.notion_payload)}'
        f"<p>{_e(_CHOICE_TEXT[choice])}</p>"
    )
    extra = f'<input type="hidden" name="choice" value="{_e(choice)}">'
    label = _ISSUE_LABELS[choice]
    return _confirm(label, body, f"/sync/issues/{issue.id}", label, csrf, extra)


def issue_choice_ok(issue: IssueRow, choice: str) -> bool:
    return choice in _issue_choices(issue)


def render_confirm_remove(row: AdminRow, csrf: str) -> str:
    label = row.info.name or f"Unknown user ({row.info.user_id})"
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div></div>'
        '<section class="card"><h3>Remove an admin</h3>'
        f"{_admin_row(row, 0, with_remove=False)}"
        f"<p>Remove {_e(label)}?</p>"
        '<p class="muted">The person loses the admin commands right away.</p>'
        '<div class="actions"><form method="post" action="/admins/remove">'
        f'{_csrf_field(csrf)}<input type="hidden" name="id" value="{_e(row.info.user_id)}">'
        '<button class="btn red" type="submit">Remove</button></form>'
        '<a class="btn g" href="/">Cancel</a></div></section>'
    )


_TOP = f'<div class="top"><div class="brand">{_BRAND}</div></div>'


def _dj_row(row: dict, actions: bool = True) -> str:
    guest = row["discord_user_id"] < 0
    tag = '<span class="tag g">Guest</span>' if guest else '<span class="tag">Discord</span>'
    link = row["stream_url_primary"] or ""
    dj_id = _e(row["discord_user_id"])
    buttons = (
        f'<a class="btn g" href="/djs/edit?id={dj_id}">Edit</a>'
        f'<a class="btn red" href="/djs/delete?id={dj_id}">Delete</a>'
        if actions
        else ""
    )
    return (
        f'<div class="row"><div class="who"><b>{_e(row["dj_name"])}</b>'
        f"<small>{_e(link)}</small></div>{tag}{buttons}</div>"
    )


def _dj_fields(values: dict[str, str], with_discord: bool) -> str:
    def field(name: str, placeholder: str, required: bool = False, maxlen: int = 500) -> str:
        req = " required" if required else ""
        return (
            f'<input class="in" type="text" name="{name}" maxlength="{maxlen}" '
            f'placeholder="{_e(placeholder)}" aria-label="{_e(placeholder)}" '
            f'value="{_e(values.get(name, ""))}" autocomplete="off"{req}>'
        )

    html = (
        field("name", "DJ name (required)", True, 80)
        + field("link", "Stream link (required)", True)
        + field("alt_link", "Backup stream link")
        + field("vrchat", "VRChat name", False, 80)
    )
    if with_discord:
        html += field(
            "discord_id", "Discord user ID (leave empty for a guest without Discord)", False, 25
        )
    return html


_LOGO_INPUT = (
    '<p class="muted">Logo (optional, PNG or JPEG, up to 5 MB)</p>'
    '<input class="in" type="file" name="logo" accept="image/png,image/jpeg" '
    'aria-label="DJ logo">'
)


def render_djs(
    rows: list[dict],
    csrf: str,
    message: ActionResult | None = None,
    values: dict[str, str] | None = None,
) -> str:
    items = "".join(_dj_row(r) for r in rows) or '<p class="muted">No DJs yet.</p>'
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div>'
        '<a class="btn g" href="/">Back</a></div>'
        f"{_message(message)}"
        '<section class="card"><h3>Add a DJ</h3>'
        f'<form method="post" action="/djs/add" enctype="multipart/form-data">{_csrf_field(csrf)}'
        f'{_dj_fields(values or {}, True)}{_LOGO_INPUT}'
        '<button class="btn" type="submit">Add DJ</button></form>'
        '<p class="muted">A DJ without a Discord user ID is a guest: the bot books them '
        "but never sends them messages.</p></section>"
        f'<section class="card"><h3>DJ list ({len(rows)})</h3>{items}</section>'
    )


def render_edit_dj(
    row: dict,
    csrf: str,
    message: ActionResult | None = None,
    values: dict[str, str] | None = None,
) -> str:
    shown = values or {
        "name": row["dj_name"] or "",
        "link": row["stream_url_primary"] or "",
        "alt_link": row["stream_url_backup"] or "",
        "vrchat": row["vrchat_name"] or "",
    }
    logo_note = "A logo is saved. Choose a file to replace it." if row["has_logo"] else ""
    return _page(
        f"{_TOP}{_message(message)}"
        '<section class="card"><h3>Edit a DJ</h3>'
        f'<form method="post" action="/djs/edit" enctype="multipart/form-data">'
        f'{_csrf_field(csrf)}<input type="hidden" name="id" value="{_e(row["discord_user_id"])}">'
        f"{_dj_fields(shown, False)}"
        f'{_LOGO_INPUT}<p class="muted">{_e(logo_note)}</p>'
        '<div class="actions"><button class="btn" type="submit">Save</button>'
        '<a class="btn g" href="/djs">Cancel</a></div></form></section>'
    )


def render_confirm_delete_dj(row: dict, bookings: list[tuple[str, str]], csrf: str) -> str:
    if bookings:
        items = "".join(f"<li>{_e(event)}, {_e(slot)}</li>" for event, slot in bookings)
        extra = f"<p>These upcoming bookings are cancelled:</p><ul>{items}</ul>"
    else:
        extra = '<p class="muted">The DJ has no upcoming bookings.</p>'
    return _page(
        f"{_TOP}"
        '<section class="card"><h3>Delete a DJ</h3>'
        f"{_dj_row(row, actions=False)}"
        f"<p>Delete {_e(row['dj_name'])}?</p>{extra}"
        '<div class="actions"><form method="post" action="/djs/delete">'
        f'{_csrf_field(csrf)}<input type="hidden" name="id" value="{_e(row["discord_user_id"])}">'
        '<button class="btn red" type="submit">Delete</button></form>'
        '<a class="btn g" href="/djs">Cancel</a></div></section>'
    )


_FIELD_CHOICES = (("genre", "Genre"), ("location", "Location"))
_NOTE_EXCERPT = 60


def _text_input(name: str, label: str, value: str, maxlen: int) -> str:
    return (
        f'<input class="in" type="text" name="{name}" maxlength="{maxlen}" '
        f'placeholder="{_e(label)}" aria-label="{_e(label)}" value="{_e(value)}" '
        'autocomplete="off">'
    )


def _labeled(label: str, control: str, hint: str = "") -> str:
    """A form control with a visible label above it and an optional hint below."""
    small = f"<small>{_e(hint)}</small>" if hint else ""
    return f'<label class="fld"><span>{_e(label)}</span>{control}{small}</label>'


def _rule_inputs(
    values: dict[str, str],
    anchor_options: list[tuple[str, str]],
    field_select: str = "",
) -> str:
    """Value, interval and first event of a rule, each with a label and a hint."""
    return (
        field_select
        + _labeled(
            "Value",
            _text_input("value", "e.g. Techno or Warehouse 9", values.get("value", ""), 80),
            "The genre or location the matching events get.",
        )
        + _labeled(
            "Every how many events",
            _every_input(values.get("every", "1")),
            "1 = every event, 2 = every second event, 3 = every third.",
        )
        + _labeled(
            "Starting with",
            _anchor_select(anchor_options, values.get("start")),
            "The first scheduled event the rule applies to.",
        )
    )


def _anchor_select(options: list[tuple[str, str]], selected: str | None = None) -> str:
    items = "".join(
        f'<option value="{_e(iso)}"{" selected" if iso == selected else ""}>{_e(label)}</option>'
        for label, iso in options
    )
    return f'<select class="in" name="start" aria-label="First event of the rule">{items}</select>'


def _every_input(value: str) -> str:
    return (
        f'<input class="in" type="number" name="every" min="1" max="52" value="{_e(value)}" '
        'aria-label="Every how many events (1 to 52)" required>'
    )


def _rule_row(view: RuleView) -> str:
    rule = view.rule
    return (
        f'<div class="row"><div class="who"><b>#{_e(rule.id)} '
        f"{_e(texts.field_name(rule.field))}: {_e(rule.value)}</b>"
        f"<small>every {_e(rule.every)} &middot; from {_e(view.label)}</small></div>"
        f'<a class="btn g" href="/schedule/rules/{_e(rule.id)}/edit">Edit</a>'
        f'<a class="btn red" href="/schedule/rules/{_e(rule.id)}/remove">Remove</a></div>'
    )


def _event_row(item) -> str:
    event = item.event
    if event is None:
        return (
            f'<div class="row"><div class="who"><b>{_e(item.label)}</b>'
            '<small>Not created yet</small></div></div>'
        )
    parts = []
    for field in ("genre", "location"):
        value = getattr(event, field)
        if value:
            by_hand = getattr(event, f"{field}_by_hand")
            hand = ' <span class="tag">set by hand</span>' if by_hand else ""
            parts.append(f"{_e(texts.field_name(field))}: {_e(value)}{hand}")
    if event.notes:
        note = event.notes.replace("\n", " ")
        if len(note) > _NOTE_EXCERPT:
            note = note[: _NOTE_EXCERPT - 1] + "\u2026"
        parts.append(f"Note: {_e(note)}")
    detail = " &middot; ".join(parts) or "nothing set"
    return (
        f'<div class="row"><div class="who"><b>{_e(item.label)}</b><small>{detail}</small></div>'
        f'<a class="btn g" href="/schedule/events/{_e(quote(event.page_id, safe=""))}">Edit</a>'
        "</div>"
    )


def render_schedule(
    overview: ScheduleOverview,
    anchor_options: list[tuple[str, str]],
    csrf: str,
    message: ActionResult | None = None,
    values: dict[str, str] | None = None,
) -> str:
    values = values or {}
    shown_defaults = {
        f: values.get(f, overview.defaults.get(f, "")) for f in ("genre", "location")
    }
    banner = ""
    if overview.pending:
        banner = (
            f'<p class="msg err" role="status">{overview.pending} upcoming '
            f'event{"s" if overview.pending != 1 else ""} differ from the rules. '
            '<a href="/schedule/apply">Review the changes</a></p>'
        )
    summary = (
        '<section class="card"><h3>Schedule</h3>'
        f'<p class="kv">Scheduled events: {"on" if overview.enabled else "off"}</p>'
        f'<p class="kv">One event every {_e(overview.interval_days)} days</p>'
        f'<p class="kv">First date: {_e(overview.first_date or "not set")}</p>'
        f'<p class="kv">Future events kept ready: {_e(overview.lookahead)}</p>'
        '<p class="muted">Change these with /setup in Discord.</p></section>'
    )
    defaults = (
        '<section class="card"><h3>Defaults</h3>'
        f'<form method="post" action="/schedule/defaults">{_csrf_field(csrf)}'
        + _labeled(
            "Default genre",
            _text_input("genre", "Default genre", shown_defaults["genre"], 40),
            "Up to 40 characters.",
        )
        + _labeled(
            "Default location",
            _text_input("location", "Default location", shown_defaults["location"], 80),
            "Up to 80 characters.",
        )
        + '<div class="actions"><button class="btn" type="submit">Save defaults</button></div>'
        "</form>"
        '<p class="muted">Used by scheduled events when no rule sets the value. '
        "Empty means none.</p></section>"
    )
    rows = "".join(_rule_row(v) for v in overview.rules) or '<p class="muted">No rules yet.</p>'
    field_options = "".join(
        f'<option value="{k}"{" selected" if values.get("field") == k else ""}>{_e(v)}</option>'
        for k, v in _FIELD_CHOICES
    )
    field_select = _labeled(
        "Sets",
        f'<select class="in" name="field" aria-label="What the rule sets">{field_options}</select>',
        "Whether the rule sets the genre or the location.",
    )
    rules = (
        f'<section class="card"><h3>Rules ({len(overview.rules)})</h3>{rows}'
        '<div class="sub"><h4>Add a rule</h4>'
        '<p class="muted">A rule sets the genre or location of every n-th scheduled event, '
        "counted from the event you pick. A value set by hand always wins.</p>"
        f'<form method="post" action="/schedule/rules/add">{_csrf_field(csrf)}'
        + _rule_inputs(values, anchor_options, field_select)
        + '<div class="actions"><button class="btn" type="submit">Add rule</button></div>'
        "</form></div></section>"
    )
    events = "".join(_event_row(d) for d in overview.dates) or '<p class="muted">None.</p>'
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div>'
        '<a class="btn g" href="/">Back</a></div>'
        f"{_message(message)}{banner}{summary}{defaults}{rules}"
        f'<section class="card"><h3>Next events</h3>{events}</section>'
    )


def _checkbox(name: str, label: str, checked: bool) -> str:
    return (
        f'<label class="row"><input type="checkbox" name="{name}" value="on" '
        f'aria-label="{_e(label)}"{" checked" if checked else ""}> '
        f'<span class="who">{_e(label)}</span></label>'
    )


def _number_input(name: str, label: str, value: str, high: int) -> str:
    return (
        f'<input class="in" type="text" name="{name}" value="{_e(value)}" '
        f'inputmode="numeric" maxlength="{len(str(high)) + 2}" placeholder="{_e(label)}" '
        f'aria-label="{_e(label)}" autocomplete="off">'
    )


def render_booking_rules(
    rules: BookingRules,
    csrf: str,
    message: ActionResult | None = None,
    values: dict[str, str] | None = None,
) -> str:
    """The Booking rules form. ``values`` are the typed form values after a refused save
    (a checkbox is on when its key is present); nothing typed is echoed unescaped."""
    if values is None:
        on_limit, on_cutoff = rules.slot_limit_enabled, rules.cancel_cutoff_enabled
        limit, cutoff = str(rules.slot_limit_max), str(rules.cancel_cutoff_minutes)
    else:
        on_limit, on_cutoff = "slot_limit_enabled" in values, "cancel_cutoff_enabled" in values
        limit, cutoff = values.get("slot_limit_max", ""), values.get("cancel_cutoff_minutes", "")
    slot_card = (
        '<section class="card"><h3>Slot limit</h3>'
        + _checkbox(
            "slot_limit_enabled", "Limit how many slots one DJ can book in the same event", on_limit
        )
        + _labeled(
            "At most N slots per DJ and event",
            _number_input("slot_limit_max", "At most N slots per DJ and event", limit, 100),
            "Existing bookings are kept. Only new bookings are refused. "
            "Applies to DJ buttons and invites. Admins can always book a DJ by hand.",
        )
        + "</section>"
    )
    cutoff_card = (
        '<section class="card"><h3>Cancellation cutoff</h3>'
        + _checkbox(
            "cancel_cutoff_enabled",
            "DJs cannot cancel in the bot shortly before the event",
            on_cutoff,
        )
        + _labeled(
            "Minutes before the event starts",
            _number_input(
                "cancel_cutoff_minutes", "Minutes before the event starts", cutoff, 10080
            ),
            "0 means DJs can cancel until the event starts. Admins can always cancel.",
        )
        + "</section>"
    )
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div>'
        '<a class="btn g" href="/">Back</a></div>'
        f"{_message(message)}"
        f'<form method="post" action="/booking-rules">{_csrf_field(csrf)}'
        f"{slot_card}{cutoff_card}"
        '<div class="actions"><button class="btn" type="submit">Save booking rules</button>'
        "</div></form>"
    )


def render_edit_rule(
    view: RuleView,
    options: list[tuple[str, str]],
    csrf: str,
    message: ActionResult | None = None,
    values: dict[str, str] | None = None,
) -> str:
    rule = view.rule
    values = values or {}
    return _page(
        f"{_TOP}{_message(message)}"
        f'<section class="card"><h3>Edit rule #{_e(rule.id)} '
        f"({_e(texts.field_name(rule.field))})</h3>"
        f'<form method="post" action="/schedule/rules/{_e(rule.id)}/edit">{_csrf_field(csrf)}'
        f'<input type="hidden" name="field" value="{_e(rule.field)}">'
        + _rule_inputs(
            {
                "value": values.get("value", rule.value),
                "every": values.get("every", str(rule.every)),
                "start": values.get("start", rule.anchor.isoformat()),
            },
            options,
        )
        + '<div class="actions"><button class="btn" type="submit">Save</button>'
        '<a class="btn g" href="/schedule">Cancel</a></div></form></section>'
    )


def render_confirm_remove_rule(view: RuleView, csrf: str) -> str:
    rule = view.rule
    return _page(
        f"{_TOP}"
        '<section class="card"><h3>Remove a rule</h3>'
        f"<p>Remove rule #{_e(rule.id)} ({_e(texts.field_name(rule.field))} "
        f'"{_e(rule.value)}", every {_e(rule.every)}, from {_e(view.label)})?</p>'
        '<p class="muted">Events keep their values until you apply the changes.</p>'
        f'<div class="actions"><form method="post" action="/schedule/rules/{_e(rule.id)}/remove">'
        f'{_csrf_field(csrf)}<button class="btn red" type="submit">Remove</button></form>'
        '<a class="btn g" href="/schedule">Cancel</a></div></section>'
    )


def render_edit_event(
    event,
    csrf: str,
    message: ActionResult | None = None,
    values: dict[str, str] | None = None,
    original: dict[str, str] | None = None,
) -> str:
    """The event form. ``original`` holds the values the form first showed (hidden
    fields), so a save only touches the fields the admin changed."""
    stored = {
        "genre": event.genre or "",
        "location": event.location or "",
        "note": event.notes or "",
    }
    shown = values or stored
    first = original or stored
    hidden = "".join(
        f'<input type="hidden" name="orig_{name}" value="{_e(first[name])}">'
        for name in ("genre", "location", "note")
    )
    return _page(
        f"{_TOP}{_message(message)}"
        f'<section class="card"><h3>{_e(texts.event_name(event))} &middot; '
        f"{_e(event.date.strftime('%d.%m.%Y'))}</h3>"
        f'<form method="post" action="/schedule/events/{_e(quote(event.page_id, safe=""))}">'
        f"{_csrf_field(csrf)}{hidden}"
        + _text_input("genre", "Genre", shown["genre"], 40)
        + _text_input("location", "Location", shown["location"], 80)
        + '<textarea class="in" name="note" rows="4" maxlength="300" placeholder="Note" '
        f'aria-label="Note">{_e(shown["note"])}</textarea>'
        '<p class="muted">An empty genre or location goes back to the rules (or stays empty '
        "on a manual event). A value you enter is kept even when rules change.</p>"
        '<div class="actions"><button class="btn" type="submit">Save</button>'
        '<a class="btn g" href="/schedule">Cancel</a></div></form></section>'
    )


def render_apply(
    changes: list[PendingView], csrf: str, message: ActionResult | None = None
) -> str:
    if not changes:
        body = (
            '<p class="muted">No upcoming event changes.</p>'
            '<a class="btn g" href="/schedule">Back</a>'
        )
    else:
        items = "".join(
            f'<label class="row"><input type="checkbox" name="pick" '
            f'value="{_e(pick_key(c.uid, c.field))}"> <span class="who">{_e(c.text)}</span></label>'
            f'<input type="hidden" name="shown" value="{_e(pick_key(c.uid, c.field))}">'
            for c in changes
        )
        body = (
            f'<p>{_e(texts.APPLY_HEADING)}</p>'
            f'<form method="post" action="/schedule/apply">{_csrf_field(csrf)}{items}'
            '<div class="actions">'
            '<button class="btn" type="submit" name="choice" value="all">'
            f"{_e(texts.APPLY_ALL)}</button>"
            f'<button class="btn" type="submit" name="choice" value="selected">'
            f"{_e(texts.APPLY_SELECTED)}</button>"
            f'<button class="btn g" type="submit" name="choice" value="keep">'
            f"{_e(texts.APPLY_KEEP)}</button></div></form>"
        )
    return _page(f"{_TOP}{_message(message)}"
                 f'<section class="card"><h3>Changes to apply</h3>{body}</section>')


def render_notice(message: str) -> str:
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div></div>'
        f'<section class="card"><p>{_e(message)}</p>'
        '<a class="btn g" href="/">Back to the admin page</a></section>'
    )


# --- event announcements ---

TEMPLATE_MAXLENGTH = 6000
_PLACEHOLDER_HELP = {
    "title": "the event name",
    "date": "start date and time, shown in each reader's time zone",
    "countdown": "time until the start, for example \"in 3 days\"",
    "description": "the event's notes",
    "notes": "the event's notes (same as {description})",
    "location": "the location (empty when none)",
    "genre": "the genre (empty when none)",
    "lineup": "one line per slot: start time and DJ, or open slot",
    "slots": "number of slots",
    "booked": "number of booked slots",
    "now_playing": "the DJ playing now (empty before the start)",
    "next_up": "the next DJ (empty after the last slot)",
    "status": "a short line: starting soon, live now or ended",
}
_KIND_TITLE = {GENERAL: "General announcement", START: "Start announcement"}
_KIND_HELP = {
    GENERAL: "Posted before the event by the rules above.",
    START: "Posted when the event starts and edited as the slots change.",
}


def _hhmm(value: datetime.time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _ann_rule_text(rule: AnnouncementRule) -> str:
    return texts.announce_rule_line(rule.id, rule.days_before, _hhmm(rule.post_time))


def _ann_input(name: str, label: str, value: str, maxlen: int, numeric: bool = False) -> str:
    mode = ' inputmode="numeric"' if numeric else ""
    return (
        f'<input class="in" type="text" name="{name}" value="{_e(value)}" '
        f'maxlength="{maxlen}"{mode} placeholder="{_e(label)}" aria-label="{_e(label)}" '
        'autocomplete="off">'
    )


def _ann_rule_inputs(values: dict[str, str]) -> str:
    return _labeled(
        "Days before the event",
        _ann_input("days", "Days before the event", values.get("days", ""), 5, True),
        "0 to 365. 0 means on the day of the event.",
    ) + _labeled(
        "Time of day",
        _ann_input("time", "Time of day, HH:MM", values.get("time", ""), 10),
        "HH:MM in the bot's time zone, for example 18:00.",
    )


def _ann_rule_row(rule: AnnouncementRule) -> str:
    return (
        f'<div class="row"><div class="who"><b>{_e(_ann_rule_text(rule))}</b></div>'
        f'<a class="btn g" href="/announcements/rules/{_e(rule.id)}/edit">Edit</a>'
        f'<a class="btn red" href="/announcements/rules/{_e(rule.id)}/remove">Remove</a></div>'
    )


def _ann_settings_card(
    data: AnnouncementPageData, csrf: str, values: dict[str, str] | None
) -> str:
    stored = data.settings
    if values is None:
        channel = str(stored.channel_id) if stored.channel_id else ""
        general_on, start_on = stored.general_enabled, stored.start_enabled
    else:
        channel = values.get("channel", "")
        general_on, start_on = GENERAL in values, START in values
    if stored.channel_id is None:
        current = "No channel is set."
    elif data.channel_name:
        current = f"Current channel: #{data.channel_name} ({stored.channel_id})"
    else:
        current = f"Current channel: {stored.channel_id} (not found on the server)"
    warn = ""
    if stored.channel_id is None and (stored.general_enabled or stored.start_enabled):
        warn = '<p class="msg err">No channel is set, so nothing is posted.</p>'
    return (
        '<section class="card"><h3>Announcement settings</h3>'
        f'<p class="kv">{_e(current)}</p>{warn}'
        f'<form method="post" action="/announcements/settings">{_csrf_field(csrf)}'
        + _labeled(
            "Channel ID",
            _ann_input("channel", "Channel ID", channel, 25, True),
            "A text channel the bot can post in. Empty clears the channel.",
        )
        + _checkbox(GENERAL, "General announcement (before the event, by the rules)", general_on)
        + _checkbox(START, "Start announcement (when the event starts)", start_on)
        + '<div class="actions"><button class="btn" type="submit">Save settings</button></div>'
        "</form>"
        '<p class="muted">Turning the general announcement on creates a default rule '
        "(3 days before at 18:00) when there are no rules. An announcement is only posted "
        "when every slot of the event is booked. Without a channel nothing is posted.</p>"
        "</section>"
    )


def _ann_rules_card(
    data: AnnouncementPageData, csrf: str, values: dict[str, str] | None
) -> str:
    rows = "".join(_ann_rule_row(r) for r in data.rules)
    if not rows:
        rows = '<p class="muted">No rules: the general announcement posts nothing.</p>'
    return (
        f'<section class="card"><h3>General announcement rules ({len(data.rules)})</h3>{rows}'
        '<div class="sub"><h4>Add a rule</h4>'
        '<p class="muted">Each rule posts one message per event: the given number of days '
        "before the event at the given time.</p>"
        f'<form method="post" action="/announcements/rules/add">{_csrf_field(csrf)}'
        + _ann_rule_inputs(values or {"days": "", "time": "18:00"})
        + '<div class="actions"><button class="btn" type="submit">Add rule</button></div>'
        "</form></div></section>"
    )


def _ann_check_panel(check: TemplateCheck, event) -> str:
    if check.ok:
        verdict = '<p class="msg ok" role="status">The template is valid and can be posted.</p>'
    else:
        items = "".join(f"<li>{_e(p)}</li>" for p in check.problems)
        verdict = (
            '<div class="msg err" role="status">The template cannot be posted.'
            f"<ul>{items}</ul></div>"
        )
    warnings = ""
    if check.warnings:
        warnings = '<ul class="kv">' + "".join(f"<li>{_e(w)}</li>" for w in check.warnings)
        warnings += "</ul>"
    left = MAX_LENGTH - check.length
    rest = f"{left} left" if left >= 0 else f"{-left} over"
    if event is None:
        source = "the sample event (there is no upcoming event)"
    else:
        source = f"the next event: {event.event_id} on {event.date.strftime('%d.%m.%Y')}"
    return (
        f'<div class="sub"><h4>Check</h4>{verdict}{warnings}'
        f'<p class="muted">{check.length} / {MAX_LENGTH} characters ({rest})</p>'
        f'<p class="muted">Preview filled with {_e(source)}. Times are Discord time codes '
        "(&lt;t:...&gt;); Discord shows them in each reader's time zone.</p>"
        f'<pre class="pv">{_e(check.preview)}</pre></div>'
    )


def _ann_template_card(
    kind: str,
    data: AnnouncementPageData,
    csrf: str,
    draft: str | None,
    check: TemplateCheck | None,
) -> str:
    stored = data.settings.general_template if kind == GENERAL else data.settings.start_template
    text = stored if draft is None else draft
    state = "the default template" if stored == DEFAULT_TEMPLATES[kind] else "a custom template"
    save = f"/announcements/templates/{kind}/save"
    names = "".join(
        f"<dt>{_e('{' + name + '}')}</dt><dd>{_e(_PLACEHOLDER_HELP.get(name, ''))}</dd>"
        for name in PLACEHOLDERS[kind]
    )
    panel = _ann_check_panel(check, data.event) if check is not None else ""
    return (
        f'<section class="card"><h3>{_e(_KIND_TITLE[kind])} template</h3>'
        f'<p class="muted">{_e(_KIND_HELP[kind])} The stored text is {state}. '
        "Discord markdown and role mentions (&lt;@&amp;role id&gt;) work; @everyone and "
        "@here never ping.</p>"
        f'<form method="post" action="{save}">{_csrf_field(csrf)}'
        f'<textarea class="in ta" name="text" rows="12" maxlength="{TEMPLATE_MAXLENGTH}" '
        f'aria-label="{_e(_KIND_TITLE[kind])} template">\n{_e(text)}</textarea>'
        '<div class="actions">'
        f'<button class="btn g" type="submit" formaction="/announcements/templates/{kind}/check">'
        "Check and preview</button>"
        '<button class="btn" type="submit">Save</button></div></form>'
        f"{panel}"
        f'<dl class="ph">{names}</dl>'
        '<p class="muted">Write {{ and }} for a literal brace. Save always runs the check '
        "first and refuses a template that cannot be posted. An empty text means the default "
        "template.</p>"
        f'<form method="post" action="{save}">{_csrf_field(csrf)}'
        '<input type="hidden" name="text" value="">'
        '<button class="btn red" type="submit">Reset to default</button></form>'
        "</section>"
    )


def render_announcements(
    data: AnnouncementPageData,
    csrf: str,
    message: ActionResult | None = None,
    *,
    settings_values: dict[str, str] | None = None,
    rule_values: dict[str, str] | None = None,
    drafts: dict[str, str] | None = None,
    checked: tuple[str, TemplateCheck] | None = None,
) -> str:
    """The Announcements page. ``drafts`` keep typed template text after a check or a
    refused save; ``checked`` is the check result shown under that type's editor."""
    drafts = drafts or {}
    cards = "".join(
        _ann_template_card(
            kind,
            data,
            csrf,
            drafts.get(kind),
            checked[1] if checked is not None and checked[0] == kind else None,
        )
        for kind in (GENERAL, START)
    )
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div>'
        '<a class="btn g" href="/">Back</a></div>'
        f"{_message(message)}"
        f"{_ann_settings_card(data, csrf, settings_values)}"
        f"{_ann_rules_card(data, csrf, rule_values)}"
        f"{cards}"
    )


def render_edit_announcement_rule(
    rule: AnnouncementRule,
    csrf: str,
    message: ActionResult | None = None,
    values: dict[str, str] | None = None,
) -> str:
    shown = values or {"days": str(rule.days_before), "time": _hhmm(rule.post_time)}
    return _page(
        f"{_TOP}{_message(message)}"
        f'<section class="card"><h3>Edit rule #{_e(rule.id)}</h3>'
        f'<form method="post" action="/announcements/rules/{_e(rule.id)}/edit">'
        f"{_csrf_field(csrf)}"
        + _ann_rule_inputs(shown)
        + '<p class="muted">A change applies to events this rule has not posted for yet.</p>'
        '<div class="actions"><button class="btn" type="submit">Save</button>'
        '<a class="btn g" href="/announcements">Cancel</a></div></form></section>'
    )


def render_confirm_remove_announcement_rule(rule: AnnouncementRule, csrf: str) -> str:
    return _page(
        f"{_TOP}"
        '<section class="card"><h3>Remove a rule</h3>'
        f"<p>Remove rule {_e(_ann_rule_text(rule))}?</p>"
        '<p class="muted">Announcements this rule already posted stay in the channel.</p>'
        f'<div class="actions"><form method="post" '
        f'action="/announcements/rules/{_e(rule.id)}/remove">'
        f'{_csrf_field(csrf)}<button class="btn red" type="submit">Remove</button></form>'
        '<a class="btn g" href="/announcements">Cancel</a></div></section>'
    )
