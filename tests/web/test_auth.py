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


def test_check_password_lone_surrogate():
    assert check_password("\ud800", "x") is False
    assert check_password("x", "\ud800") is False
    assert check_password("\ud800", "\ud800") is True


def test_csrf_ok_lone_surrogate():
    mgr = SessionManager(clock=Clock())
    _, session = mgr.create()
    assert mgr.csrf_ok(session, "\ud800") is False


def test_throttle_eviction_keeps_blocked_key():
    clock = Clock()
    t = LoginThrottle(clock=clock, max_entries=100)
    for _ in range(5):
        t.record_failure("victim")
    assert t.blocked_for("victim") > 0
    for i in range(1000):
        clock.now += 0.01
        t.record_failure(f"flood{i}")
    assert t.blocked_for("victim") > 0
    assert len(t._entries) <= 100


def test_throttle_all_blocked_table_not_evicted():
    t = LoginThrottle(clock=Clock(), max_entries=3)
    for k in "abc":
        for _ in range(5):
            t.record_failure(k)
    for _ in range(5):
        t.record_failure("d")
    assert all(t.blocked_for(k) > 0 for k in "abcd")


def test_throttle_prune_is_rate_limited_but_blocked_for_exact():
    clock = Clock()
    t = LoginThrottle(clock=clock, max_entries=10_000)
    for _ in range(5):
        t.record_failure("a")
    t.record_failure("b")
    clock.now += 301  # block and window over, but under the prune interval rule
    assert t.blocked_for("a") == 0.0
    clock.now = 1000.0 + 1  # irrelevant; check no scan below interval
    t2 = LoginThrottle(clock=clock, max_entries=10_000)
    t2.record_failure("x")
    clock.now += 1
    t2.record_failure("y")
    assert set(t2._entries) == {"x", "y"}


def test_throttle_pruning_bounds_table():
    clock = Clock()
    t = LoginThrottle(clock=clock, max_entries=100)
    for i in range(2000):
        clock.now += 1
        t.record_failure(f"k{i}")
    assert len(t._entries) <= 100


def test_revoked_session_no_longer_verifies() -> None:
    from bot.web.auth import SessionManager

    sessions = SessionManager(secret=b"k" * 32)
    token, _ = sessions.create()
    other, _ = sessions.create()
    sessions.revoke(token)
    assert sessions.verify(token) is None
    assert sessions.verify(other) is not None
    sessions.revoke("garbage")  # ignored
