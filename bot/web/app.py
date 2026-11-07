"""aiohttp application for the admin page: routes, session and security middlewares.

Notes:
- The session cookie cannot carry ``Secure``: the page is served over plain http on the
  local network. It is ``HttpOnly``, ``SameSite=Strict`` and signed.
- Sessions are stateless signed tokens, so logout clears the cookie in the browser only;
  a copied token stays valid until it expires.
- The throttle key is ``request.remote`` only; ``X-Forwarded-For`` is never trusted.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from multidict import MultiDictProxy

from bot.web.auth import (
    COOKIE_NAME,
    SESSION_LIFETIME_SECONDS,
    LoginThrottle,
    SessionManager,
    check_password,
)
from bot.web.pages import render_confirm_remove, render_dashboard, render_login, render_notice
from bot.web.service import FORMAT_MESSAGE, ActionResult

logger = logging.getLogger(__name__)

MAX_BODY = 4096
WRONG_PASSWORD = "Wrong password."
OWNER_MESSAGE = "Owners can only be changed with ADMIN_USER_IDS."
NOT_ADDED_MESSAGE = "This person is not an added admin."
CSRF_MESSAGE = "This form has expired. Go back, reload the page and try again."

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
        page = render_dashboard(service.server_info(), rows, request["session"].csrf, message)
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

    app = web.Application(
        middlewares=[security_middleware, session_middleware], client_max_size=MAX_BODY
    )
    app.router.add_get("/login", get_login)
    app.router.add_post("/login", post_login)
    app.router.add_post("/logout", post_logout)
    app.router.add_get("/", get_index)
    app.router.add_get("/admins/remove", get_remove)
    app.router.add_post("/admins/remove", post_remove)
    app.router.add_post("/admins/add", post_add)
    return app
