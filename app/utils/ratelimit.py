"""
Lightweight in-process per-IP rate limiting for the HTTP API.

A fixed-window counter keyed by client IP, exposed as a Starlette middleware.
No external dependency and no Redis round-trip — suitable for protecting the
public (unauthenticated) API surface from abuse / accidental hammering on a
single instance. For a multi-instance deployment a shared store would be needed;
this is a pragmatic first line of defence, gated by config.

The decision core (``FixedWindowLimiter``) is pure and unit-tested.
"""

from __future__ import annotations

import time
from typing import Sequence

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.utils.observability import request_id_ctx

# Bound the IP table so a flood of unique IPs cannot grow memory without limit.
_MAX_TRACKED_IPS = 50_000


class FixedWindowLimiter:
    """Fixed-window per-key limiter: at most ``limit`` hits per ``window_sec``.

    ``allow(key, now)`` returns ``(allowed, retry_after_sec)``. Memory is bounded
    to the number of keys seen in the current window (with opportunistic pruning
    of stale windows).
    """

    def __init__(self, limit: int, window_sec: int = 60) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_sec))
        self._state: dict[str, list[int]] = {}  # key -> [window_index, count]

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        now = time.time() if now is None else now
        w = int(now // self.window)
        entry = self._state.get(key)
        if entry is None or entry[0] != w:
            if len(self._state) >= _MAX_TRACKED_IPS:
                self._prune(w)
            self._state[key] = [w, 1]
            return True, 0
        if entry[1] < self.limit:
            entry[1] += 1
            return True, 0
        retry_after = self.window - int(now % self.window)
        return False, max(1, retry_after)

    def _prune(self, current_window: int) -> None:
        stale = [k for k, v in self._state.items() if v[0] != current_window]
        for k in stale:
            del self._state[k]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests over the per-IP limit on matching path prefixes with 429."""

    def __init__(
        self,
        app,
        *,
        limit: int,
        window_sec: int = 60,
        prefixes: Sequence[str] = ("/api/public/",),
        enabled: bool = True,
    ) -> None:
        super().__init__(app)
        self._limiter = FixedWindowLimiter(limit, window_sec)
        self._prefixes = tuple(p for p in prefixes if p)
        self._enabled = enabled and bool(self._prefixes)

    def _client_ip(self, request: Request) -> str:
        # Honour a single proxy hop's X-Forwarded-For first entry if present.
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "-"

    async def dispatch(self, request: Request, call_next):
        if self._enabled and request.url.path.startswith(self._prefixes):
            ip = self._client_ip(request)
            allowed, retry_after = self._limiter.allow(ip)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "rate_limited",
                        "detail": "Too many requests — slow down.",
                        "correlation_id": request_id_ctx.get(),
                    },
                    headers={"Retry-After": str(retry_after)},
                )
        return await call_next(request)
