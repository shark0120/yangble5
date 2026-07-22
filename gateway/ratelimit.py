"""In-process rate limiting, concurrency capping, auth-failure backoff and the
KDF verification cache.

WHY in-process and not Redis: the gateway's job is to protect one operator's
wallet, and adding a network dependency to the hot path adds a failure mode
(what does the gateway do when Redis is down? fail open and get drained, or
fail closed and go down?) for a benefit that only matters above a few hundred
requests per second. The honest trade-off: **these limiters are per worker
process**. Run uvicorn with one worker, or divide the limits by the worker
count. The durable protections — daily budgets and the global monthly cap —
live in SQLite and are shared correctly across workers regardless.

Everything here is keyed by an opaque id (key_id or an IP *hash*), never by a
secret and never by a raw address.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "AuthCache",
    "ConcurrencyLimiter",
    "FailureBackoff",
    "SlidingWindowLimiter",
    "TimedThrottle",
    "UpstreamHealth",
]

Clock = Callable[[], float]


class SlidingWindowLimiter:
    """Requests-per-window limiter with an exact sliding window.

    Exact rather than token-bucket because the operator-facing promise is "60
    requests per minute", and a bucket lets a burst of 120 through at a window
    boundary.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0, clock: Clock = time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, ident: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds). Records the hit when allowed."""
        if self.limit <= 0:
            return True, 0.0
        now = self._clock()
        with self._lock:
            hits = self._hits.get(ident)
            if hits is None:
                hits = self._hits[ident] = deque()
            cutoff = now - self.window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(0.0, hits[0] + self.window - now)
            hits.append(now)
            return True, 0.0

    def remaining(self, ident: str) -> int:
        if self.limit <= 0:
            return -1
        now = self._clock()
        with self._lock:
            hits = self._hits.get(ident)
            if not hits:
                return self.limit
            cutoff = now - self.window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            return max(0, self.limit - len(hits))

    def prune(self) -> None:
        now = self._clock()
        with self._lock:
            for ident in [i for i, h in self._hits.items() if not h or h[-1] <= now - self.window]:
                self._hits.pop(ident, None)


class ConcurrencyLimiter:
    """Caps simultaneous in-flight requests per key.

    This is what bounds *quota overshoot*: a budget can only be checked before a
    request and charged after it, so N concurrent requests can each pass the
    check just under the limit. Capping N caps the overshoot.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, ident: str) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            current = self._active.get(ident, 0)
            if current >= self.limit:
                return False
            self._active[ident] = current + 1
            return True

    def release(self, ident: str) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            current = self._active.get(ident, 0) - 1
            if current > 0:
                self._active[ident] = current
            else:
                self._active.pop(ident, None)

    def active(self, ident: str) -> int:
        with self._lock:
            return self._active.get(ident, 0)


class UpstreamHealth:
    """Rolling-window view of whether the SHARED-POOL upstream is actually serving.

    WHY this exists at all: every other capacity signal in this gateway is a
    *budget* signal — spend counters divided by configured ceilings. When the one
    account behind the shared pool starts refusing (402, 429, 403, 5xx), no usage
    row is written, no spend is added, and every budget ratio therefore stays
    exactly where it was. The public capacity widget would keep reporting "100%
    remaining" through a total outage, because remaining-budget and
    service-working are different questions and only one of them was being asked.

    Deliberately NOT a circuit breaker. It never refuses a request, so the
    upstream is always given the chance to recover on its own; it only reports.
    A breaker that stopped sending traffic would also stop observing successes
    and would need half-open logic to ever reset, which is a much larger thing to
    get wrong on a single-account service.

    `record_success` clears the window outright: one request that worked is
    stronger evidence than several older ones that did not.
    """

    def __init__(
        self,
        window_seconds: float = 120.0,
        failure_threshold: int = 3,
        clock: Clock = time.monotonic,
    ):
        self.window = window_seconds
        self.failure_threshold = max(1, failure_threshold)
        self._clock = clock
        self._failures: deque[float] = deque()
        self._last_status: int | None = None
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()

    def record_failure(self, status: int) -> None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._failures.append(now)
            self._last_status = status

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._last_status = None

    def healthy(self) -> bool:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return len(self._failures) < self.failure_threshold

    def snapshot(self) -> tuple[bool, int | None, int]:
        """(healthy, last_failure_status, failures_in_window)."""
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            count = len(self._failures)
            return count < self.failure_threshold, self._last_status, count


class FailureBackoff:
    """Exponential-ish lockout after repeated auth failures from one IP.

    Slows down both key brute-forcing and invite-code guessing. Keyed by IP
    hash; a success clears the counter.
    """

    def __init__(self, threshold: int, lockout_seconds: int, clock: Clock = time.monotonic):
        self.threshold = threshold
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._state: dict[str, tuple[int, float]] = {}  # ident -> (failures, locked_until)
        self._lock = threading.Lock()

    def locked_for(self, ident: str) -> float:
        if self.threshold <= 0:
            return 0.0
        with self._lock:
            _failures, until = self._state.get(ident, (0, 0.0))
            return max(0.0, until - self._clock())

    def record_failure(self, ident: str) -> float:
        """Return the seconds the caller is now locked out for (0 = not locked)."""
        if self.threshold <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            failures, until = self._state.get(ident, (0, 0.0))
            failures += 1
            if failures >= self.threshold:
                # Double the penalty for every failure past the threshold, capped
                # at an hour so a shared NAT egress is never bricked forever.
                over = failures - self.threshold
                penalty = min(self.lockout_seconds * (2 ** min(over, 6)), 3600)
                until = now + penalty
            self._state[ident] = (failures, until)
            return max(0.0, until - now)

    def record_success(self, ident: str) -> None:
        with self._lock:
            self._state.pop(ident, None)


class TimedThrottle:
    """A soft, self-clearing "slow down" mark on an identifier.

    Used for the loose machine-binding limits. The operator asked for LOOSE
    enforcement, and this class is what "loose" means in code: exceeding
    MAX_IPS_PER_KEY sets a deadline a minute or so out and nothing else. No row
    is written, no status is changed, no key is suspended, and the mark
    evaporates on its own. A user who tethered their laptop, then used office
    wifi, then a train hotspot is inconvenienced for a minute — they are not
    locked out, and they never have to email anybody to be un-banned.

    In-process like the rest of this module: worst case after a restart is that
    someone gets one un-throttled minute, which is the correct direction to be
    wrong in for a limit whose purpose is politeness rather than protection.
    """

    def __init__(self, clock: Clock = time.monotonic):
        self._clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def throttle(self, ident: str, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = self._clock() + seconds
        with self._lock:
            # Never shorten an existing throttle by re-marking it.
            self._until[ident] = max(self._until.get(ident, 0.0), deadline)

    def remaining(self, ident: str) -> float:
        """Seconds still to wait; 0.0 when not throttled."""
        now = self._clock()
        with self._lock:
            until = self._until.get(ident)
            if until is None:
                return 0.0
            if until <= now:
                del self._until[ident]
                return 0.0
            return until - now

    def clear(self, ident: str) -> None:
        with self._lock:
            self._until.pop(ident, None)


@dataclass(frozen=True)
class _CacheEntry:
    verifier: bytes
    expires_at: float


class AuthCache:
    """Caches the *result* of a successful KDF verification, nothing else.

    scrypt costs tens of milliseconds and ~16 MiB by design. Paying that on
    every proxied request would make the gateway trivially CPU-DoS-able, so a
    successful verification is remembered for a short TTL.

    Two deliberate restrictions:
      * only successes are cached, so a wrong secret always costs the attacker a
        full KDF (and trips FailureBackoff);
      * the key *status* and *budgets* are NOT cached — they are re-read from
        SQLite on every request, so suspending a key takes effect immediately.

    The cached value is an HMAC of the secret under a per-process random pepper,
    so a memory dump yields no reusable credential and comparison stays
    constant-time.
    """

    def __init__(self, ttl_seconds: int, clock: Clock = time.monotonic):
        self.ttl = ttl_seconds
        self._clock = clock
        self._pepper = secrets.token_bytes(32)
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def _verifier(self, secret: str) -> bytes:
        return hmac.new(self._pepper, secret.encode("utf-8"), hashlib.sha256).digest()

    def check(self, key_id: str, secret: str) -> bool:
        """True only if this exact secret was verified recently for this key_id."""
        if self.ttl <= 0:
            return False
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return False
            if entry.expires_at <= now:
                self._entries.pop(key_id, None)
                return False
            expected = entry.verifier
        return hmac.compare_digest(expected, self._verifier(secret))

    def store(self, key_id: str, secret: str) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._entries[key_id] = _CacheEntry(
                verifier=self._verifier(secret), expires_at=self._clock() + self.ttl
            )

    def invalidate(self, key_id: str) -> None:
        with self._lock:
            self._entries.pop(key_id, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
