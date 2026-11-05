"""Rate limiting: login brute-force protection and per-connection request budgets."""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple


class TokenBucket:
    """``rate`` tokens per second, at most ``burst`` saved up."""

    __slots__ = ("rate", "burst", "tokens", "t")

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = float(rate), float(burst)
        self.tokens = float(burst)
        self.t = time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class LoginThrottle:
    """Failed logins lock out the client address and the account name separately.

    After ``max_ip`` failures from one address (or ``max_user`` failures for one
    account, from anywhere) further attempts are refused - without checking the
    password, so a locked-out attacker learns nothing - for ``base`` seconds,
    doubling with every further lockout up to ``cap``.  Failures are forgotten
    after ``window`` seconds without one.  A successful login clears the address
    (not the account: a distributed attack keeps the account protected).
    """

    def __init__(self, max_ip: int = 5, max_user: int = 10, base: float = 30.0,
                 cap: float = 3600.0, window: float = 900.0, max_entries: int = 100_000):
        self.max_ip, self.max_user = max_ip, max_user
        self.base, self.cap, self.window = base, cap, window
        self.max_entries = max_entries
        # key -> [failures, last_failure, locked_until, lockouts]
        self._s: Dict[Tuple[str, str], list] = {}

    def _get(self, key) -> Optional[list]:
        e = self._s.get(key)
        if e is None:
            return None
        now = time.time()
        if e[2] <= now and now - e[1] > self.window:
            del self._s[key]
            return None
        return e

    def blocked(self, ip: str, user: str) -> float:
        """Seconds until a login attempt is allowed again (0 = now)."""
        now = time.time()
        wait = 0.0
        for key in (("ip", ip), ("user", user.lower())):
            e = self._get(key)
            if e is not None and e[2] > now:
                wait = max(wait, e[2] - now)
        return wait

    def failure(self, ip: str, user: str) -> None:
        now = time.time()
        if len(self._s) > self.max_entries:
            self._prune(now)
        for key, limit in ((("ip", ip), self.max_ip), (("user", user.lower()), self.max_user)):
            e = self._get(key)
            if e is None:
                e = self._s[key] = [0, now, 0.0, 0]
            e[0] += 1
            e[1] = now
            if e[0] >= limit:
                e[3] += 1
                e[2] = now + min(self.cap, self.base * 2 ** (e[3] - 1))
                e[0] = 0

    def success(self, ip: str, user: str) -> None:
        self._s.pop(("ip", ip), None)

    def _prune(self, now: float) -> None:
        for k in [k for k, e in self._s.items() if e[2] <= now and now - e[1] > self.window]:
            del self._s[k]
        if len(self._s) > self.max_entries:      # still full: forget the oldest half
            for k, _ in sorted(self._s.items(), key=lambda kv: kv[1][1])[: len(self._s) // 2]:
                del self._s[k]
