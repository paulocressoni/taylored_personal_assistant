"""Unit tests for the pure SlidingWindowLimiter (BE-11) — no ENV needed."""

import pytest

from app.api.ratelimit import SlidingWindowLimiter


def test_allows_until_limit_then_rejects() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True


def test_window_expires_old_events(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deterministic clock instead of sleeping.
    clock = {"now": 0.0}
    monkeypatch.setattr("app.api.ratelimit.time.monotonic", lambda: clock["now"])
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    assert limiter.allow("k") is True
    clock["now"] = 5.0
    assert limiter.allow("k") is False  # still inside the window
    clock["now"] = 11.0
    assert limiter.allow("k") is True  # window has rolled over


def test_disabled_when_limit_zero() -> None:
    limiter = SlidingWindowLimiter(limit=0, window_seconds=60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True


def test_clear_resets_all_keys() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    limiter.clear()
    assert limiter.allow("k") is True
