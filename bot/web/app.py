"""aiohttp application for the admin page: routes, session and security middlewares.

Notes:
- The session cookie cannot carry ``Secure``: the page is served over plain http on the
  local network. It is ``HttpOnly``, ``SameSite=Strict`` and signed.
- Sessions are stateless signed tokens, so logout clears the cookie in the browser only;
  a copied token stays valid until it expires.
- The throttle key is ``request.remote`` only; ``X-Forwarded-For`` is never trusted.
- The app accepts bodies up to ``MAX_BACKUP`` for the backup upload only; the security
  middleware lowers every other request to ``MAX_UPLOAD`` (logo forms) or ``MAX_BODY``.
- Backup import, rebuild and the import retry are awaited in the request (aiohttp keeps
  serving other requests and the bot keeps running); the service holds no database
  connection across them.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from multidict import MultiDictProxy

from bot.services.dj_admin import parse_discord_id
from bot.ui import texts
from bot.web.auth import (
    COOKIE_NAME,
    SESSION_LIFETIME_SECONDS,
    LoginThrottle,
    SessionManager,
    check_password,
)
from bot.web.pages import (
    issue_choice_ok,
    render_backup_import,
    render_confirm_delete_dj,
    render_confirm_issue,
    render_confirm_rebuild,
    render_confirm_recreate,
    render_confirm_remove,
    render_confirm_resync,
    render_confirm_retry_import,
    render_dashboard,
    render_djs,
    render_edit_dj,
    render_login,
    render_notice,
)
from bot.web.service import FORMAT_MESSAGE, ActionResult

logger = logging.getLogger(__name__)

MAX_BODY = 4096
MAX_UPLOAD = 6 * 1024 * 1024  # a DJ logo (up to 5 MB) plus the other form fields
MAX_BACKUP = 512 * 1024 * 1024  # a backup file for /backup/import
BACKUP_PATH = "/backup/import"
UPLOAD_PATHS = frozenset({"/djs/add", "/djs/edit", BACKUP_PATH})
ADMIN_NAME = "The admin page"
WRONG_PASSWORD = "Wrong password."
OWNER_MESSAGE = "Owners can only be changed with ADMIN_USER_IDS."
NOT_ADDED_MESSAGE = "This person is not an added admin."
NO_DJ_MESSAGE = "There is no DJ with this ID."
BOOKINGS_FAILED = "Could not read the bookings. Try again."
CSRF_MESSAGE = "This form has expired. Go back, reload the page and try again."
NO_BACKUP_FILE = "Choose a backup file to restore."
EXPORT_FAILED = "The backup could not be made. Try again or check the bot log."
NO_ISSUE_MESSAGE = "This conflict or warning is no longer open."
WRONG_CHOICE_MESSAGE = "This choice does not fit this entry."

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; img-src https://cdn.discordapp.com; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}

_PUBLIC = {("GET", "/login"), ("HEAD", "/login"), ("POST", "/login")}
_SAFE_METHODS = {"GET", "HEAD"}
_ERROR_TEXT = {
    400: "The request could not be read.",
    404: "Page not found.",
    405: "This action is not available here.",
    413: "The request is too large.",
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _html(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/html", charset="utf-8")


def _redirect(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location})


def _from_http_exception(exc: web.HTTPException) -> web.Response:
    location = getattr(exc, "location", None)
    if 300 <= exc.status < 400 and location is not None:
        return _redirect(str(location))
    text = _ERROR_TEXT.get(exc.status, "Something went wrong.")
    resp = _html(render_notice(text), exc.status)
    if "Allow" in exc.headers:
        resp.headers["Allow"] = exc.headers["Allow"]
    return resp


@web.middleware
async def security_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        if request.method not in _SAFE_METHODS:
            length = request.content_length or 0
            if request.path not in UPLOAD_PATHS and (
                length > MAX_BODY
                or "chunked" in request.headers.get("Transfer-Encoding", "").lower()
            ):
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=length)
            limit = MAX_BACKUP if request.path == BACKUP_PATH else MAX_UPLOAD
            if length > limit:
                raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=length)
            if limit != MAX_BACKUP:
                # The app allows MAX_BACKUP for the backup upload; every other body
                # (also a chunked logo upload) is read with the smaller limit.
                request = request.clone(client_max_size=limit)
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = _from_http_exception(exc)
    except Exception:
        logger.exception("Admin page request failed: %s %s", request.method, request.path)
        resp = _html(render_notice("Something went wrong. Try again."), 500)
    resp.headers.update(SECURITY_HEADERS)
    return resp


async def _read_form(request: web.Request) -> MultiDictProxy[Any]:
    """The parsed form; 413 for oversized bodies, 400 for malformed ones."""
    try:
        return await request.post()
    except web.HTTPException:
        raise
    except Exception:
        raise web.HTTPBadRequest() from None


def _field(form: MultiDictProxy[Any], name: str) -> str | None:
    value = form.get(name)
    return value if isinstance(value, str) else None


def _notion_problem(service: Any) -> str | None:
    """The Notion problem of the service (test fakes may not have the method)."""
    getter = getattr(service, "notion_problem", None)
    return getter() if callable(getter) else None


async def _sync_status(service: Any) -> Any:
    """The sync status for the dashboard, or None (test fakes, failed read)."""
    getter = getattr(service, "sync_status", None)
    if not callable(getter):
        return None
    try:
        return await getter()
    except Exception:
        logger.warning("Could not read the sync status for the admin page", exc_info=True)
        return None


def _backup_filename() -> str:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    return f"ravebot-{stamp}.ravebot.json.gz"


async def _upload_bytes(form: MultiDictProxy[Any], name: str) -> bytes | None:
    """The uploaded file (aiohttp keeps it in a temporary file until read), or None."""
    value = form.get(name)
    file = getattr(value, "file", None)
    if file is None or not getattr(value, "filename", ""):
        return None
    try:
        data = await asyncio.to_thread(file.read)
    finally:
        await asyncio.to_thread(file.close)
    return data or None


def _maintenance(service: Any) -> ActionResult | None:
    """The refusal for a write while an import replaces the data, or None."""
    getter = getattr(service, "maintenance", None)
    text = getter() if callable(getter) else None
    return ActionResult(False, text) if text else None


def _logo_bytes(form: MultiDictProxy[Any]) -> bytes | None:
    """The uploaded logo file, or None when no file was chosen."""
    value = form.get("logo")
    file = getattr(value, "file", None)
    if file is None or not getattr(value, "filename", ""):
        return None
    return file.read() or None


_DJ_FIELDS = ("name", "link", "alt_link", "vrchat", "discord_id")


def _dj_values(form: MultiDictProxy[Any]) -> dict[str, str]:
    return {name: (_field(form, name) or "") for name in _DJ_FIELDS}


def create_app(
    service: Any,
    password: str,
    *,
    sessions: SessionManager | None = None,
    throttle: LoginThrottle | None = None,
    fail_delay: float = 0.5,
) -> web.Application:
    """Build the admin page app. ``service`` is an ``AdminPageService`` (or a compatible fake)."""
    sessions = sessions if sessions is not None else SessionManager()
    throttle = throttle if throttle is not None else LoginThrottle()

    @web.middleware
    async def session_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        session = sessions.verify(request.cookies.get(COOKIE_NAME))
        if (request.method, request.path) in _PUBLIC:
            request["session"] = session
            return await handler(request)
        if session is None:
            return _redirect("/login")
        request["session"] = session
        if request.method not in _SAFE_METHODS:
            form = await _read_form(request)
            if not sessions.csrf_ok(session, _field(form, "csrf")):
                return _html(render_notice(CSRF_MESSAGE), 403)
        return await handler(request)

    async def dashboard(
        request: web.Request, message: ActionResult | None = None, status: int = 200
    ) -> web.Response:
        rows = await service.list_admins()
        problem = _notion_problem(service)
        sync = await _sync_status(service)
        paused = getattr(service, "maintenance", None)
        page = render_dashboard(
            service.server_info(), rows, request["session"].csrf, message, problem, sync,
            notion_paused=bool(paused()) if callable(paused) else False,
        )
        return _html(page, status)

    async def get_login(request: web.Request) -> web.Response:
        if request["session"] is not None:
            return _redirect("/")
        return _html(render_login())

    async def post_login(request: web.Request) -> web.Response:
        key = request.remote or "unknown"
        # Read the body first. From blocked_for to record_failure/record_success there
        # is no await, so parallel requests cannot all pass the block check at once.
        given = _field(await _read_form(request), "password")
        blocked = throttle.blocked_for(key)
        if blocked > 0:
            minutes = max(1, math.ceil(blocked / 60))
            unit = "minute" if minutes == 1 else "minutes"
            error = f"Too many attempts. Try again in {minutes} {unit}."
            await asyncio.sleep(fail_delay)
            return _html(render_login(error), 429)
        if given is None:
            return _html(render_login("Enter the password."), 400)
        if not check_password(given, password):
            throttle.record_failure(key)
            logger.info("Admin page login failed from %s", key)
            await asyncio.sleep(fail_delay)
            return _html(render_login(WRONG_PASSWORD), 401)
        throttle.record_success(key)
        token, _ = sessions.create()
        resp = _redirect("/")
        resp.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_LIFETIME_SECONDS,
            path="/",
            httponly=True,
            samesite="Strict",
        )
        return resp

    async def post_logout(request: web.Request) -> web.Response:
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        resp = _redirect("/login")
        resp.del_cookie(COOKIE_NAME, path="/", httponly=True, samesite="Strict")
        return resp

    async def get_index(request: web.Request) -> web.Response:
        return await dashboard(request)

    async def get_remove(request: web.Request) -> web.Response:
        raw = request.query.get("id", "")
        row = await service.admin_row(raw)
        if row is None:
            return await dashboard(request, ActionResult(False, NOT_ADDED_MESSAGE), 400)
        if row.is_owner:
            return await dashboard(request, ActionResult(False, OWNER_MESSAGE), 400)
        return _html(render_confirm_remove(row, request["session"].csrf))

    async def post_action(
        request: web.Request, action: Callable[[str], Awaitable[ActionResult]]
    ) -> web.Response:
        raw = _field(await _read_form(request), "id")
        if raw is None:
            return await dashboard(request, ActionResult(False, FORMAT_MESSAGE), 400)
        result = await action(raw)
        return await dashboard(request, result, 200 if result.ok else 400)

    async def post_add(request: web.Request) -> web.Response:
        return await post_action(request, service.add_admin)

    async def post_remove(request: web.Request) -> web.Response:
        return await post_action(request, service.remove_admin)

    # --- DJ list ---

    async def djs_page(
        request: web.Request,
        message: ActionResult | None = None,
        status: int = 200,
        values: dict[str, str] | None = None,
    ) -> web.Response:
        rows = await service.djs.list_djs()
        return _html(render_djs(rows, request["session"].csrf, message, values), status)

    async def get_djs(request: web.Request) -> web.Response:
        return await djs_page(request)

    async def post_dj_add(request: web.Request) -> web.Response:
        if refused := _maintenance(service):
            return await djs_page(request, refused, 503)
        form = await _read_form(request)
        values = _dj_values(form)
        raw_discord = values["discord_id"].strip()
        discord_id = parse_discord_id(raw_discord) if raw_discord else None
        if raw_discord and discord_id is None:
            return await djs_page(
                request, ActionResult(False, FORMAT_MESSAGE), 400, values
            )
        result = await service.djs.add_dj(
            admin_name=ADMIN_NAME,
            name=values["name"],
            link=values["link"],
            alt_link=values["alt_link"],
            vrchat=values["vrchat"],
            discord_id=discord_id,
            logo=_logo_bytes(form),
        )
        keep = None if result.ok else values
        return await djs_page(request, ActionResult(result.ok, result.message),
                              200 if result.ok else 400, keep)

    async def _dj_or_none(raw: str | None) -> dict | None:
        try:
            dj_id = int(raw or "")
        except ValueError:
            return None
        return await service.djs.get_dj(dj_id)

    async def get_dj_edit(request: web.Request) -> web.Response:
        row = await _dj_or_none(request.query.get("id"))
        if row is None:
            return await djs_page(request, ActionResult(False, NO_DJ_MESSAGE), 404)
        return _html(render_edit_dj(row, request["session"].csrf))

    async def post_dj_edit(request: web.Request) -> web.Response:
        if refused := _maintenance(service):
            return await djs_page(request, refused, 503)
        form = await _read_form(request)
        row = await _dj_or_none(_field(form, "id"))
        if row is None:
            return await djs_page(request, ActionResult(False, NO_DJ_MESSAGE), 404)
        values = _dj_values(form)
        result = await service.djs.edit_dj(
            admin_name=ADMIN_NAME,
            dj_id=row["discord_user_id"],
            fields={k: values[k] for k in ("name", "link", "alt_link", "vrchat")},
            logo=_logo_bytes(form),
        )
        if not result.ok:
            page = render_edit_dj(
                row, request["session"].csrf, ActionResult(False, result.message), values
            )
            return _html(page, 400)
        return await djs_page(request, ActionResult(True, result.message))

    async def get_dj_delete(request: web.Request) -> web.Response:
        row = await _dj_or_none(request.query.get("id"))
        if row is None:
            return await djs_page(request, ActionResult(False, NO_DJ_MESSAGE), 404)
        try:
            found = await service.djs.future_bookings(row["discord_user_id"])
        except Exception:
            logger.warning("Could not list the bookings of a DJ", exc_info=True)
            return await djs_page(request, ActionResult(False, BOOKINGS_FAILED), 502)
        bookings = [(e.event_id, f"slot {i + 1} at {texts.plain_time(e, i)}") for e, i in found]
        return _html(render_confirm_delete_dj(row, bookings, request["session"].csrf))

    async def post_dj_delete(request: web.Request) -> web.Response:
        if refused := _maintenance(service):
            return await djs_page(request, refused, 503)
        row = await _dj_or_none(_field(await _read_form(request), "id"))
        if row is None:
            return await djs_page(request, ActionResult(False, NO_DJ_MESSAGE), 404)
        result = await service.djs.delete_dj(
            admin_name=ADMIN_NAME, dj_id=row["discord_user_id"]
        )
        return await djs_page(
            request, ActionResult(result.ok, result.message), 200 if result.ok else 400
        )

    # --- Notion databases ---

    async def get_notion_recreate(request: web.Request) -> web.Response:
        problem = _notion_problem(service)
        if not problem:
            return _redirect("/")
        pending = getattr(service, "notion_import_pending", None)
        import_pending = bool(await pending()) if callable(pending) else False
        return _html(
            render_confirm_recreate(problem, request["session"].csrf, import_pending)
        )

    async def post_notion_recreate(request: web.Request) -> web.Response:
        result = await service.recreate_notion()
        return await dashboard(request, result, 200 if result.ok else 400)

    # --- data and backup ---

    async def get_backup_export(request: web.Request) -> web.StreamResponse:
        try:
            data = await service.export_backup()
        except Exception:
            logger.exception("The backup export for the admin page failed")
            return await dashboard(request, ActionResult(False, EXPORT_FAILED), 500)
        return web.Response(
            body=data,
            content_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{_backup_filename()}"'
            },
        )

    async def get_backup_import(request: web.Request) -> web.Response:
        return _html(render_backup_import(request["session"].csrf))

    async def post_backup_import(request: web.Request) -> web.Response:
        data = await _upload_bytes(await _read_form(request), "backup")
        if data is None:
            return await dashboard(request, ActionResult(False, NO_BACKUP_FILE), 400)
        result = await service.import_backup(data)
        del data
        return await dashboard(request, result, 200 if result.ok else 400)

    # --- Notion sync (hidden without Notion, decision 10) ---

    async def _sync_enabled() -> bool:
        sync = await _sync_status(service)
        return sync is not None and sync.enabled

    async def get_sync_rebuild(request: web.Request) -> web.Response:
        if not await _sync_enabled():
            return _redirect("/")
        return _html(render_confirm_rebuild(request["session"].csrf))

    async def post_sync_rebuild(request: web.Request) -> web.Response:
        result = await service.rebuild_from_notion()
        return await dashboard(request, result, 200 if result.ok else 400)

    async def get_sync_resync(request: web.Request) -> web.Response:
        if not await _sync_enabled():
            return _redirect("/")
        return _html(render_confirm_resync(request["session"].csrf))

    async def post_sync_resync(request: web.Request) -> web.Response:
        result = await service.resync_all()
        return await dashboard(request, result, 200 if result.ok else 400)

    async def get_sync_retry(request: web.Request) -> web.Response:
        sync = await _sync_status(service)
        if sync is None or not sync.problem or not (sync.enabled or sync.can_retry):
            return _redirect("/")
        return _html(render_confirm_retry_import(sync.problem, request["session"].csrf))

    async def post_sync_retry(request: web.Request) -> web.Response:
        result = await service.retry_import()
        return await dashboard(request, result, 200 if result.ok else 400)

    async def get_sync_issue(request: web.Request) -> web.Response:
        if not await _sync_enabled():
            return _redirect("/")
        issue = await service.open_issue(request.match_info["id"])
        if issue is None:
            return await dashboard(request, ActionResult(False, NO_ISSUE_MESSAGE), 404)
        choice = request.query.get("choice", "")
        if not issue_choice_ok(issue, choice):
            return await dashboard(request, ActionResult(False, WRONG_CHOICE_MESSAGE), 400)
        return _html(render_confirm_issue(issue, choice, request["session"].csrf))

    async def post_sync_issue(request: web.Request) -> web.Response:
        choice = _field(await _read_form(request), "choice") or ""
        result = await service.resolve_issue(request.match_info["id"], choice)
        return await dashboard(request, result, 200 if result.ok else 400)

    app = web.Application(
        middlewares=[security_middleware, session_middleware], client_max_size=MAX_BACKUP
    )
    app.router.add_get("/login", get_login)
    app.router.add_post("/login", post_login)
    app.router.add_post("/logout", post_logout)
    app.router.add_get("/", get_index)
    app.router.add_get("/admins/remove", get_remove)
    app.router.add_post("/admins/remove", post_remove)
    app.router.add_post("/admins/add", post_add)
    app.router.add_get("/notion/recreate", get_notion_recreate)
    app.router.add_post("/notion/recreate", post_notion_recreate)
    app.router.add_get("/djs", get_djs)
    app.router.add_post("/djs/add", post_dj_add)
    app.router.add_get("/djs/edit", get_dj_edit)
    app.router.add_post("/djs/edit", post_dj_edit)
    app.router.add_get("/djs/delete", get_dj_delete)
    app.router.add_post("/djs/delete", post_dj_delete)
    app.router.add_get("/backup/export", get_backup_export)
    app.router.add_get(BACKUP_PATH, get_backup_import)
    app.router.add_post(BACKUP_PATH, post_backup_import)
    app.router.add_get("/sync/rebuild", get_sync_rebuild)
    app.router.add_post("/sync/rebuild", post_sync_rebuild)
    app.router.add_get("/sync/resync", get_sync_resync)
    app.router.add_post("/sync/resync", post_sync_resync)
    app.router.add_get("/sync/retry-import", get_sync_retry)
    app.router.add_post("/sync/retry-import", post_sync_retry)
    app.router.add_get(r"/sync/issues/{id:\d{1,18}}", get_sync_issue)
    app.router.add_post(r"/sync/issues/{id:\d{1,18}}", post_sync_issue)
    return app
