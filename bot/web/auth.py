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
    return hmac.compare_digest(
        given.encode("utf-8", errors="surrogatepass"),
        expected.encode("utf-8", errors="surrogatepass"),
    )


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
        # csrf -> expiry of sessions ended by logout; a copied cookie stops working too
        self._revoked: dict[str, float] = {}

    def revoke(self, token: str | None) -> None:
        session = self.verify(token)
        if session is None:
            return
        now = self._clock()
        self._revoked = {c: e for c, e in self._revoked.items() if e > now}
        self._revoked[session.csrf] = session.expires

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
        if csrf in self._revoked:
            return None
        return Session(csrf=csrf, expires=expires)

    def csrf_ok(self, session: Session, form_token: str | None) -> bool:
        if not form_token:
            return False
        return hmac.compare_digest(
            form_token.encode("utf-8", errors="surrogatepass"),
            session.csrf.encode("utf-8", errors="surrogatepass"),
        )


class LoginThrottle:
    """Per key failure counter with a temporary block, bounded in memory."""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 300,
        block_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 10_000,
        prune_interval: float = 5.0,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._block = block_seconds
        self._clock = clock
        self._max_entries = max_entries
        self._prune_interval = prune_interval
        self._last_prune = clock()
        # key -> (failure timestamps inside the window, blocked_until or 0.0)
        self._entries: dict[str, tuple[list[float], float]] = {}

    def _prune(self, now: float, force: bool = False) -> None:
        # Full scans are rate limited: only when the table is above 80 percent
        # of the cap or the interval has passed. blocked_for does not depend on
        # pruning, so expired blocks still read as 0.0 immediately.
        if not force and (
            len(self._entries) <= 0.8 * self._max_entries
            and now - self._last_prune < self._prune_interval
        ):
            return
        self._last_prune = now
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
        if len(self._entries) > self._max_entries:
            self._prune(now, force=True)
        # Still over the cap: evict the unblocked entry with the oldest last
        # failure (dict order is last update order). Entries with an active
        # block are never evicted. If every entry is blocked, nothing is
        # evicted and the table temporarily exceeds the cap; blocks expire and
        # are pruned, and each blocked key cost its owner max_failures attempts.
        while len(self._entries) > self._max_entries:
            victim = next(
                (k for k, (_, until) in self._entries.items() if k != key and until <= now),
                None,
            )
            if victim is None:
                break
            del self._entries[victim]

    def record_success(self, key: str) -> None:
        self._entries.pop(key, None)
