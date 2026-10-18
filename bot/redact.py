"""Secret redaction — masks known secret values in text and log output.

Masking is by value: callers pass the secrets they know (DSNs, API keys, tokens).
For a DSN the full string, user, password and percent-decoded password are collected.
Values shorter than MIN_SECRET_LEN are ignored so ordinary words are not corrupted
(a password shorter than that is therefore NOT masked on its own).
Masking the DSN user also masks that word elsewhere: with user `postgres`, the text
`postgresql` in a log line becomes `***ql`. This is accepted to keep the user out of logs.

[Source: prd.md#NFR5, FR43]
[Source: story 8-1-mask-credentials-in-migration-errors.md]
"""
import logging
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit

MASK = "***"
MIN_SECRET_LEN = 4


def secret_values(*secrets: str) -> list[str]:
    """Expand DSNs/tokens into every string form that must be masked, longest first."""
    found: set[str] = set()
    for secret in secrets:
        if not isinstance(secret, str):
            continue
        found.add(secret.strip())
        try:
            parts = urlsplit(secret)
            password, username = parts.password, parts.username
        except ValueError:
            continue
        for part in (password, username):
            if part:
                found.update((part, unquote(part)))
    return sorted((s for s in found if len(s) >= MIN_SECRET_LEN), key=len, reverse=True)


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace each secret in text with '***' (longest first). Never raises."""
    try:
        if not isinstance(text, str):
            return text
        for secret in sorted(
            (s for s in secrets if isinstance(s, str) and s), key=len, reverse=True
        ):
            text = text.replace(secret, MASK)
    except Exception:  # noqa: BLE001 - redaction must never break error reporting
        pass
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that masks secrets in the final line (message, args and traceback)."""

    def __init__(self, secrets: Iterable[str], *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)
