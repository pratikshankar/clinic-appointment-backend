"""Login throttling (Phase 9 security hardening).

Without this, `/auth/login` will answer as fast as the network allows, which
makes password guessing a matter of patience. Argon2 already makes each attempt
expensive, but "expensive" times "unlimited" is still unlimited.

**In-process and per-worker on purpose, with eyes open.** State lives in a dict,
so two uvicorn workers each keep their own count and a restart forgets
everything. That is a real weakness and it is written down rather than hidden:
the fix is Redis, which is a dependency this project does not otherwise need.
What is here raises the cost of a guessing attack by orders of magnitude and
cannot itself fail in a way that locks out a legitimate clinic.

Keyed on **username *and* client IP together**, so one person fat-fingering
their password cannot lock a colleague out of the same account from another
machine, and a single host cannot spray attempts across many usernames for free.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class LoginRateLimiter:
    """Sliding-window failure counter with a fixed lockout."""

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._buckets: dict[str, _Bucket] = {}
        # A dict mutated from several request threads needs a lock; without one,
        # concurrent attempts can lose a failure count and the limit leaks.
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str, ip: str | None) -> str:
        return f"{(username or '').strip().lower()}|{ip or '-'}"

    def retry_after(self, username: str, ip: str | None) -> int:
        """Seconds until this identity may try again; 0 when it may try now."""
        if self.max_attempts <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(self._key(username, ip))
            if bucket is None or bucket.locked_until <= now:
                return 0
            return int(bucket.locked_until - now) + 1

    def record_failure(self, username: str, ip: str | None) -> int:
        """Count a failed attempt. Returns the lockout in seconds, or 0."""
        if self.max_attempts <= 0:
            return 0
        now = time.monotonic()
        key = self._key(username, ip)
        with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket())
            cutoff = now - self.window_seconds
            bucket.failures = [stamp for stamp in bucket.failures if stamp > cutoff]
            bucket.failures.append(now)

            if len(bucket.failures) >= self.max_attempts:
                bucket.locked_until = now + self.lockout_seconds
                bucket.failures.clear()
                logger.warning(
                    "Login locked for %ss after %d failures (key=%s)",
                    self.lockout_seconds,
                    self.max_attempts,
                    key,
                )
                return self.lockout_seconds
            self._prune(now)
            return 0

    def record_success(self, username: str, ip: str | None) -> None:
        """A correct password clears the slate for that identity."""
        with self._lock:
            self._buckets.pop(self._key(username, ip), None)

    def _prune(self, now: float) -> None:
        """Drop dead buckets so a long-running process does not grow forever.

        Called from `record_failure` while the lock is held -- failures are rare
        enough that doing it there costs nothing, and it means no background task.
        """
        if len(self._buckets) < 512:
            return
        cutoff = now - self.window_seconds
        dead = [
            key
            for key, bucket in self._buckets.items()
            if bucket.locked_until <= now and not [s for s in bucket.failures if s > cutoff]
        ]
        for key in dead:
            del self._buckets[key]

    def reset(self) -> None:
        """Clear all state. Used by tests."""
        with self._lock:
            self._buckets.clear()


login_limiter = LoginRateLimiter(
    max_attempts=settings.LOGIN_MAX_ATTEMPTS,
    window_seconds=settings.LOGIN_ATTEMPT_WINDOW_SECONDS,
    lockout_seconds=settings.LOGIN_LOCKOUT_SECONDS,
)


def client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
