from bot.services.dj_validation import (
    MAX_LINK_LENGTH,
    MAX_NAME_LENGTH,
    MAX_VRCHAT_LENGTH,
    clean_text,
    name_error,
    names_equal,
    normalize_link,
    stream_link_ok,
)


def test_limits() -> None:
    assert MAX_NAME_LENGTH == 80
    assert MAX_VRCHAT_LENGTH == 80
    assert MAX_LINK_LENGTH == 500


def test_stream_link_ok_true() -> None:
    assert stream_link_ok("twitch.tv/x")
    assert stream_link_ok("https://www.YouTube.com/c/x")
    assert stream_link_ok("rtspt://stream.vrcdn.live/live/x")


def test_stream_link_ok_false() -> None:
    assert not stream_link_ok("example.com/live")
    assert not stream_link_ok("")


def test_normalize_link() -> None:
    assert normalize_link("twitch.tv/x") == "https://twitch.tv/x"
    assert normalize_link("http://twitch.tv/x") == "http://twitch.tv/x"
    assert normalize_link("  HTTPS://Twitch.tv/x ") == "HTTPS://Twitch.tv/x"


def test_normalize_link_keeps_any_scheme() -> None:
    assert normalize_link("rtspt://stream.vrcdn.live/x") == "rtspt://stream.vrcdn.live/x"
    assert normalize_link("  rtmp://stream.vrcdn.live/x ") == "rtmp://stream.vrcdn.live/x"


def test_clean_text() -> None:
    assert clean_text("  Neon   Fox ") == "Neon Fox"


def test_names_equal() -> None:
    assert names_equal("Neon Fox", " neon  FOX ")
    assert not names_equal("Neon Fox", "Neon Fax")


def test_name_error() -> None:
    assert name_error("") == "empty"
    assert name_error("   ") == "empty"
    assert name_error("x" * 81) == "too_long"
    assert name_error("x" * 80) is None
