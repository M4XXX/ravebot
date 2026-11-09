"""Server rendered HTML for the admin page (space theme, no JavaScript, inline CSS)."""

from __future__ import annotations

from html import escape

from bot.web.service import ActionResult, AdminRow, ServerInfo

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
form{margin:0}
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


def render_dashboard(
    server: ServerInfo | None,
    rows: list[AdminRow],
    csrf: str,
    message: ActionResult | None = None,
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
    )


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


def render_notice(message: str) -> str:
    return _page(
        f'<div class="top"><div class="brand">{_BRAND}</div></div>'
        f'<section class="card"><p>{_e(message)}</p>'
        '<a class="btn g" href="/">Back to the admin page</a></section>'
    )
