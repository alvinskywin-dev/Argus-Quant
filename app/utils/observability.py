"""
Production observability primitives: request-correlation IDs, structured
access logging, and an in-process metrics registry.

The registry is intentionally dependency-free (no prometheus_client) so the
existing text-exposition `/metrics` endpoint can render it without adding a
runtime dependency. All counters live in-process and reset on restart, which
is the correct semantics for a Prometheus `counter` scraped by an external
collector.
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.utils.logger import logger

# Correlation id for the in-flight request; "-" when no request is active
# (background tasks, startup, scanner loop, etc).
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"

# Paths that should not emit an access log line or inflate request metrics —
# health/metrics probes are high-frequency and low-signal.
_QUIET_PATHS = frozenset({"/metrics", "/health", "/healthz", "/favicon.ico"})


class _Metrics:
    """Minimal in-process metrics. Single-threaded asyncio access, so plain
    dict mutation is safe without locking."""

    def __init__(self) -> None:
        self.http_requests_total: dict[str, int] = {}  # keyed by status class: 2xx/4xx/5xx
        self.http_requests_errors_total = 0
        self.http_request_duration_sum = 0.0
        self.http_request_duration_count = 0
        self.http_requests_in_flight = 0

    def observe_request(self, status_code: int, duration_s: float) -> None:
        bucket = f"{status_code // 100}xx"
        self.http_requests_total[bucket] = self.http_requests_total.get(bucket, 0) + 1
        if status_code >= 500:
            self.http_requests_errors_total += 1
        self.http_request_duration_sum += duration_s
        self.http_request_duration_count += 1

    def snapshot(self) -> dict:
        count = self.http_request_duration_count
        avg = (self.http_request_duration_sum / count) if count else 0.0
        return {
            "http_requests_total": dict(self.http_requests_total),
            "http_requests_errors_total": self.http_requests_errors_total,
            "http_requests_in_flight": self.http_requests_in_flight,
            "http_request_duration_sum": round(self.http_request_duration_sum, 6),
            "http_request_duration_count": count,
            "http_request_duration_avg_ms": round(avg * 1000, 3),
        }


METRICS = _Metrics()


def current_request_id() -> str:
    """Correlation id of the in-flight request, or '-' outside a request."""
    return request_id_ctx.get()


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Assigns a correlation id per request, binds it to the logging context,
    records latency/status metrics, and emits a structured access log line.

    Honours an inbound ``X-Request-ID`` (set by an upstream proxy/load
    balancer) so a single id flows across the whole hop; otherwise mints one.
    Always echoes the id back in the response header for client-side tracing.
    """

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        rid = incoming[:64] if incoming else _short_id()
        token = request_id_ctx.set(rid)

        path = request.url.path
        quiet = path in _QUIET_PATHS
        if not quiet:
            METRICS.http_requests_in_flight += 1

        start = time.perf_counter()
        status_code = 500
        try:
            with logger.contextualize(request_id=rid):
                response = await call_next(request)
                status_code = response.status_code
                response.headers[REQUEST_ID_HEADER] = rid
                return response
        finally:
            duration_s = time.perf_counter() - start
            if not quiet:
                METRICS.http_requests_in_flight -= 1
                METRICS.observe_request(status_code, duration_s)
                _log_access(request, status_code, duration_s, rid)
            request_id_ctx.reset(token)


def _log_access(request: Request, status_code: int, duration_s: float, rid: str) -> None:
    client = request.client.host if request.client else "-"
    ms = round(duration_s * 1000, 1)
    line = (
        f"{request.method} {request.url.path} -> {status_code} " f"{ms}ms client={client} rid={rid}"
    )
    if status_code >= 500:
        logger.error(line)
    elif status_code >= 400:
        logger.warning(line)
    else:
        logger.info(line)


__all__ = [
    "CorrelationMiddleware",
    "METRICS",
    "REQUEST_ID_HEADER",
    "current_request_id",
    "request_id_ctx",
]
