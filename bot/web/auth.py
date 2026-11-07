"""Authentication primitives for the admin page: password check, sessions, login throttle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

COOKIE_NAME = "ravebot_admin"
SESSION_LIFETIME_SECONDS = 12 * 3600

_MAX_TOKEN_LENGTH = 512


def check_password(given: str, expected: str) -> bool:
    """Constant time password comparison; an empty expected password never matches."""
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


@dataclass(frozen=True)
class Session:
    csrf: str
    expires: float


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionManager:
    """Stateless signed sessions.

    Token format: ``b64url(expires|csrf) "." b64url(hmac_sha256(secret, first part))``.
    The "." separator cannot occur in base64url output, so the format is unambiguous.
    """

    def __init__(
        self,
        secret: bytes | None = None,
        lifetime: float = SESSION_LIFETIME_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self._lifetime = lifetime
        self._clock = clock

    def _sign(self, payload_b64: str) -> bytes:
        return hmac.new(self._secret, payload_b64.encode("ascii"), hashlib.sha256).digest()

    def create(self) -> tuple[str, Session]:
        session = Session(
            csrf=secrets.token_urlsafe(32),
            expires=self._clock() + self._lifetime,
        )
        payload = _b64(f"{session.expires!r}|{session.csrf}".encode("ascii"))
        return f"{payload}.{_b64(self._sign(payload))}", session

    def verify(self, token: str | None) -> Session | None:
        try:
            return self._verify(token)
        except Exception:
            return None

    def _verify(self, token: str | None) -> Session | None:
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            return None
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts
        # Signature first, over the raw text; nothing is parsed until it matches.
        given_sig = sig_b64.encode("ascii")
        expected_sig = _b64(self._sign(payload_b64)).encode("ascii")
        if not hmac.compare_digest(given_sig, expected_sig):
            return None
        expires_text, sep, csrf = _unb64(payload_b64).decode("ascii").partition("|")
        if not sep or not csrf:
            return None
        expires = float(expires_text)
        if not self._clock() < expires:
            return None
        return Session(csrf=csrf, expires=expires)

    def csrf_ok(self, session: Session, form_token: str | None) -> bool:
        if not form_token:
            return False
        return hmac.compare_digest(form_token.encode("utf-8"), session.csrf.encode("utf-8"))


class LoginThrottle:
    """Per key failure counter with a temporary block, bounded in memory."""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 300,
        block_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 10_000,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._block = block_seconds
        self._clock = clock
        self._max_entries = max_entries
        # key -> (failure timestamps inside the window, blocked_until or 0.0)
        self._entries: dict[str, tuple[list[float], float]] = {}

    def _prune(self, now: float) -> None:
        dead = [
            key
            for key, (failures, blocked_until) in self._entries.items()
            if blocked_until <= now and (not failures or failures[-1] <= now - self._window)
        ]
        for key in dead:
            del self._entries[key]

    def blocked_for(self, key: str) -> float:
        now = self._clock()
        entry = self._entries.get(key)
        if entry is None:
            return 0.0
        return max(0.0, entry[1] - now)

    def record_failure(self, key: str) -> None:
        now = self._clock()
        self._prune(now)
        failures, blocked_until = self._entries.pop(key, ([], 0.0))
        failures = [t for t in failures if t > now - self._window]
        failures.append(now)
        if len(failures) >= self._max_failures:
            blocked_until = now + self._block
            failures = []
        self._entries[key] = (failures, blocked_until)  # re-insert: newest last
        while len(self._entries) > self._max_entries:
            del self._entries[next(iter(self._entries))]

    def record_success(self, key: str) -> None:
        self._entries.pop(key, None)
