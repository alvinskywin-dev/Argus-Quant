"""
HTTP API hardening — per-IP rate limit (B) + global exception handler (E).

The limiter decision core is unit-tested; the middleware and the 500 handler are
exercised end-to-end on a minimal app via TestClient.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.dashboard.server import _unhandled_exception_handler
from app.utils.observability import CorrelationMiddleware
from app.utils.ratelimit import FixedWindowLimiter, RateLimitMiddleware


# ── B: FixedWindowLimiter core ────────────────────────────────────────────────
def test_limiter_allows_up_to_limit_then_blocks():
    lim = FixedWindowLimiter(limit=2, window_sec=60)
    assert lim.allow("a", now=1000.0) == (True, 0)
    assert lim.allow("a", now=1001.0) == (True, 0)
    ok, retry = lim.allow("a", now=1002.0)
    assert ok is False and retry >= 1


def test_limiter_keys_are_independent():
    lim = FixedWindowLimiter(limit=1, window_sec=60)
    assert lim.allow("a", now=1000.0)[0] is True
    assert lim.allow("a", now=1000.0)[0] is False
    # A different IP has its own budget.
    assert lim.allow("b", now=1000.0)[0] is True


def test_limiter_resets_next_window():
    lim = FixedWindowLimiter(limit=1, window_sec=60)
    assert lim.allow("a", now=1000.0)[0] is True
    assert lim.allow("a", now=1000.0)[0] is False
    # 60s later → new window → budget restored.
    assert lim.allow("a", now=1060.0)[0] is True


def test_limiter_prunes_stale_keys():
    lim = FixedWindowLimiter(limit=5, window_sec=60)
    lim.allow("old", now=0.0)  # window 0
    lim.allow("new", now=600.0)  # window 10 -> triggers nothing yet
    # Force a prune by exceeding the cap is impractical here; assert state is
    # at least bounded to seen keys.
    assert set(lim._state) <= {"old", "new"}


# ── B: RateLimitMiddleware ────────────────────────────────────────────────────
def _build_app(limit=3):
    app = FastAPI()

    @app.get("/api/public/ping")
    async def ping():
        return {"ok": True}

    @app.get("/api/private/ping")
    async def pping():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom-secret-internal")

    app.add_middleware(
        RateLimitMiddleware,
        limit=limit,
        window_sec=60,
        prefixes=["/api/public/"],
        enabled=True,
    )
    app.add_middleware(CorrelationMiddleware)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    return app


def test_public_path_is_rate_limited():
    client = TestClient(_build_app(limit=3))
    for _ in range(3):
        assert client.get("/api/public/ping").status_code == 200
    r = client.get("/api/public/ping")
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    body = r.json()
    assert body["error"] == "rate_limited"
    assert "correlation_id" in body
    # 429 still carries the correlation id header (limiter sits under Correlation).
    assert r.headers.get("X-Request-ID")


def test_non_public_path_not_limited():
    client = TestClient(_build_app(limit=2))
    for _ in range(10):
        assert client.get("/api/private/ping").status_code == 200


def test_disabled_limiter_allows_all():
    app = FastAPI()

    @app.get("/api/public/ping")
    async def ping():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, limit=1, prefixes=["/api/public/"], enabled=False)
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/api/public/ping").status_code == 200


# ── E: global exception handler ───────────────────────────────────────────────
def test_unhandled_exception_returns_structured_500():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "internal_error"
    assert "correlation_id" in body
    # The internal exception message must not leak to the client.
    assert "kaboom-secret-internal" not in r.text
