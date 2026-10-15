"""In-process sliding-window rate limiter.

Good enough for a single uvicorn process (the deployment target of this project). With several
workers/instances each process keeps its own counters, so effective limits multiply; put a reverse
proxy limiter (e.g. nginx limit_req) in front if you scale out.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from app.config import get_settings
from app.errors import AppError


class RateLimiter:
    """Sliding window per (bucket, key).

    Memory is bounded by expiring idle keys (a key whose newest hit is older than its window holds no
    state worth keeping). Counters are NEVER cleared globally: doing so would let an attacker reset
    their own lockout just by creating many keys.
    """

    SWEEP_EVERY_SECONDS = 30.0

    def __init__(self) -> None:
        self._hits: dict[str, tuple[deque[float], int]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()
        self.enabled = get_settings().rate_limit_enabled

    def _sweep(self, now: float) -> None:
        dead = [k for k, (dq, window) in self._hits.items() if not dq or dq[-1] <= now - window]
        for k in dead:
            del self._hits[k]
        self._last_sweep = now

    def check(self, bucket: str, key: str, limit: int, window_seconds: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        k = f"{bucket}:{key}"
        with self._lock:
            if now - self._last_sweep >= self.SWEEP_EVERY_SECONDS:
                self._sweep(now)
            entry = self._hits.get(k)
            if entry is None:
                entry = (deque(), window_seconds)
                self._hits[k] = entry
            dq = entry[0]
            while dq and dq[0] <= now - window_seconds:
                dq.popleft()
            if len(dq) >= limit:
                retry = int(dq[0] + window_seconds - now) + 1
                raise AppError(
                    429,
                    "rate_limited",
                    "Demasiadas solicitudes. Intenta de nuevo más tarde.",
                    headers={"Retry-After": str(retry)},
                )
            dq.append(now)

    def size(self) -> int:
        with self._lock:
            return len(self._hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()

# bucket -> (limit, window seconds). Keys are the client IP, except "login_user" (the lower-cased
# username, across all IPs: slows down distributed guessing against one account).
LIMITS: dict[str, tuple[int, int]] = {
    "create_order": (10, 600),
    "order_quote": (60, 60),
    "upload_proof": (10, 600),
    "order_status": (120, 60),
    "public_read": (300, 60),
    "login": (10, 300),
    "login_user": (30, 300),
    "refresh": (60, 300),
}


def hit(bucket: str, key: str) -> None:
    limit, window = LIMITS[bucket]
    limiter.check(bucket, key, limit, window)
