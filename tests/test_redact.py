"""Tests for bot.redact — secret masking helpers and the redacting log formatter."""
import logging
import sys

from bot.redact import RedactingFormatter, redact, secret_values

DSN = "postgresql://ravebot:S3cr3t%40Pw@db.example:5432/ravebot"


class TestSecretValues:
    def test_dsn_parts_collected(self):
        values = secret_values(DSN)
        assert DSN in values
        assert "S3cr3t%40Pw" in values
        assert "S3cr3t@Pw" in values
        assert "ravebot" in values

    def test_percent_decoded_username_collected(self):
        values = secret_values("postgresql://svc%40corp:pw-12345@h/db")
        assert "svc%40corp" in values
        assert "svc@corp" in values

    def test_plain_token_collected(self):
        assert secret_values("tok-123456") == ["tok-123456"]

    def test_no_password_dsn(self):
        values = secret_values("postgresql://localhost/test")
        assert "postgresql://localhost/test" in values

    def test_unparseable_dsn_only_full_string(self):
        bad = "postgresql://[bad:pw@host/db"
        assert secret_values(bad) == [bad]

    def test_short_and_empty_ignored(self):
        assert secret_values("", "abc", "   ") == []

    def test_longest_first(self):
        values = secret_values(DSN)
        assert values == sorted(values, key=len, reverse=True)


class TestRedact:
    def test_full_dsn_masked_before_parts(self):
        text = f"could not connect to {DSN}"
        assert redact(text, secret_values(DSN)) == "could not connect to ***"

    def test_password_forms_masked(self):
        out = redact("pw=S3cr3t@Pw enc=S3cr3t%40Pw", secret_values(DSN))
        assert "S3cr3t" not in out

    def test_never_raises(self):
        assert redact(None, ["secret-value"]) is None  # type: ignore[arg-type]
        assert redact("text", [None]) == "text"  # type: ignore[list-item]

    def test_no_secrets_unchanged(self):
        assert redact("hello", []) == "hello"


class TestRedactingFormatter:
    def _record(self, msg, args=(), exc_info=None):
        return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)

    def test_masks_message_args(self):
        fmt = RedactingFormatter(["S3cr3t@Pw"], "%(message)s")
        out = fmt.format(self._record("pw %s", ("S3cr3t@Pw",)))
        assert out == "pw ***"

    def test_masks_traceback(self):
        fmt = RedactingFormatter(["S3cr3t@Pw"], "%(message)s")
        try:
            raise RuntimeError("bad S3cr3t@Pw")
        except RuntimeError:
            record = self._record("failed", exc_info=sys.exc_info())
        out = fmt.format(record)
        assert "S3cr3t@Pw" not in out
        assert "RuntimeError" in out
