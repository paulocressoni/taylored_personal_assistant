"""In-memory sliding-window rate limiter.

Leaf module: pure stdlib (``time``, ``threading``, ``collections``), imports
nothing from the app, so it is unit-testable without ENV and adds NO new
dependency (no Redis for dev). Deliberately in-memory: counts live in this
process only, reset on restart, and are not shared across workers — the right
trade-off for the single-process dev deployment; revisit (Redis/…) before
scaling horizontally.
"""

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    """Sliding-window counter keyed by an arbitrary string.

    Tracks, per key, the timestamps of accepted events within a rolling
    window. A call is allowed when fewer than ``limit`` events are recorded
    in the last ``window_seconds``; otherwise it is rejected. Old timestamps
    are pruned lazily on each call, so memory stays bounded by
    ``limit`` x (number of active keys).

    Thread-safe: mutations happen under a lock because FastAPI may reach the
    same instance from several threads (event loop + executor).
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record one event for ``key``; True when it is within budget.

        Args:
            key (str): The identity to limit on (e.g. the presented API key).

        Returns:
            bool: True if the event is allowed, False when the key has
                already used its ``limit`` events in the current window.
                A non-positive ``limit`` disables the limiter (always True).
        """
        if self.limit <= 0:
            return True  # disabled via RATE_LIMIT_REQUESTS=0
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            # Prune timestamps that have fallen out of the window.
            while hits and now - hits[0] >= self.window_seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def clear(self) -> None:
        """Drop all recorded hits (used by tests between cases)."""
        with self._lock:
            self._hits.clear()
