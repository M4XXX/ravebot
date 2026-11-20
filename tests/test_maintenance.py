"""Tests for bot/maintenance.py: one data-replacing operation at a time."""
import pytest

from bot import maintenance


def test_second_claim_is_refused_until_the_first_ends() -> None:
    first = maintenance.try_claim("rebuild")
    assert first is not None
    with first:
        assert maintenance.running() == "rebuild"
        assert maintenance.try_claim("import") is None
    assert maintenance.running() is None
    again = maintenance.try_claim("import")
    assert again is not None
    with again:
        pass


def test_claim_is_released_on_error() -> None:
    claim = maintenance.try_claim("upgrade")
    assert claim is not None
    with pytest.raises(RuntimeError), claim:
        raise RuntimeError("boom")
    assert maintenance.running() is None
