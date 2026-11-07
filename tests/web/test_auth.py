import pytest

from bot.web.auth import (
    COOKIE_NAME,
    LoginThrottle,
    SessionManager,
    check_password,
)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_cookie_name():
    assert COOKIE_NAME == "ravebot_admin"


def test_check_password():
    assert check_password("secret", "secret")
    assert not check_password("wrong", "secret")
    assert not check_password("", "")
    assert not check_password("x", "")
    assert check_password("pässwörd-✓", "pässwörd-✓")
    assert not check_password("passwort", "pässwörd")


def test_session_round_trip():
    mgr = SessionManager(clock=Clock())
    token, session = mgr.create()
    assert mgr.verify(token) == session
    assert session.csrf


def test_sessions_have_distinct_csrf():
    mgr = SessionManager(clock=Clock())
    assert mgr.create()[1].csrf != mgr.create()[1].csrf


def test_flipped_character_rejected():
    mgr = SessionManager(secret=b"s" * 32, clock=Clock())
    token, _ = mgr.create()
    for i in range(len(token)):
        flipped = token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1 :]
        assert mgr.verify(flipped) is None


def test_other_secret_rejected():
    clock = Clock()
    token, _ = SessionManager(secret=b"a" * 32, clock=clock).create()
    assert SessionManager(secret=b"b" * 32, clock=clock).verify(token) is None


def test_truncated_rejected():
    mgr = SessionManager(clock=Clock())
    token, _ = mgr.create()
    assert mgr.verify(token[:-5]) is None
    assert mgr.verify(token[: len(token) // 2]) is None


@pytest.mark.parametrize(
    "bad",
    [None, "", ".", "..", "a.b", "é" * 50, "A" * 100_000, "=" * 10, "a.b.c", "\x00", "💥"],
)
def test_verify_never_raises(bad):
    assert SessionManager(clock=Clock()).verify(bad) is None


def test_expired_rejected():
    clock = Clock()
    mgr = SessionManager(lifetime=100, clock=clock)
    token, _ = mgr.create()
    clock.now += 99
    assert mgr.verify(token) is not None
    clock.now += 1
    assert mgr.verify(token) is None


def test_csrf_ok():
    mgr = SessionManager(clock=Clock())
    _, session = mgr.create()
    assert mgr.csrf_ok(session, session.csrf)
    assert not mgr.csrf_ok(session, "wrong")
    assert not mgr.csrf_ok(session, None)
    assert not mgr.csrf_ok(session, "")
    assert not mgr.csrf_ok(session, "é")


def test_throttle_blocks_on_fifth_failure():
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for _ in range(4):
        t.record_failure("ip")
    assert t.blocked_for("ip") == 0.0
    t.record_failure("ip")
    assert t.blocked_for("ip") == 300


def test_throttle_counts_down_and_expires():
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for _ in range(5):
        t.record_failure("ip")
    clock.now += 100
    assert t.blocked_for("ip") == pytest.approx(200)
    clock.now += 200
    assert t.blocked_for("ip") == 0.0
    # a fresh failure after the block starts from zero
    t.record_failure("ip")
    assert t.blocked_for("ip") == 0.0


def test_throttle_success_resets():
    t = LoginThrottle(clock=Clock())
    for _ in range(4):
        t.record_failure("ip")
    t.record_success("ip")
    t.record_failure("ip")
    assert t.blocked_for("ip") == 0.0


def test_throttle_window_forgets_old_failures():
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for _ in range(4):
        t.record_failure("ip")
    clock.now += 301
    t.record_failure("ip")
    assert t.blocked_for("ip") == 0.0


def test_throttle_keys_independent():
    t = LoginThrottle(clock=Clock())
    for _ in range(5):
        t.record_failure("a")
    assert t.blocked_for("a") > 0
    assert t.blocked_for("b") == 0.0


def test_throttle_prunes_expired_entries():
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for i in range(50):
        t.record_failure(f"k{i}")
    clock.now += 1000
    t.record_failure("fresh")
    assert t._entries.keys() == {"fresh"}


def test_throttle_caps_size():
    t = LoginThrottle(clock=Clock(), max_entries=100)
    for i in range(500):
        t.record_failure(f"k{i}")
    assert len(t._entries) <= 100
    assert "k499" in t._entries
