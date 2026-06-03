"""Phase 6 — observability: correlation IDs, access metrics, log binding."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.utils.observability import (
    REQUEST_ID_HEADER,
    CorrelationMiddleware,
    _Metrics,
    current_request_id,
)


def test_metrics_buckets_by_status_class():
    m = _Metrics()
    m.observe_request(200, 0.010)
    m.observe_request(204, 0.020)
    m.observe_request(404, 0.005)
    m.observe_request(503, 0.030)

    snap = m.snapshot()
    assert snap["http_requests_total"] == {"2xx": 2, "4xx": 1, "5xx": 1}
    assert snap["http_requests_errors_total"] == 1  # only the 5xx
    assert snap["http_request_duration_count"] == 4
    assert snap["http_request_duration_avg_ms"] == pytest.approx(16.25, abs=0.01)


def test_metrics_avg_is_zero_when_empty():
    assert _Metrics().snapshot()["http_request_duration_avg_ms"] == 0.0


def _client():
    from starlette.testclient import TestClient

    async def ok(_request):
        # Correlation id must be visible to handlers via the context var.
        return PlainTextResponse(current_request_id())

    app = Starlette(routes=[Route("/ok", ok)])
    app.add_middleware(CorrelationMiddleware)
    return TestClient(app)


def test_middleware_mints_request_id_and_echoes_header():
    resp = _client().get("/ok")
    assert resp.status_code == 200
    rid = resp.headers[REQUEST_ID_HEADER]
    assert rid and rid != "-"
    # Handler saw the same id that was echoed back in the header.
    assert resp.text == rid


def test_middleware_honours_inbound_request_id():
    resp = _client().get("/ok", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "trace-abc-123"
    assert resp.text == "trace-abc-123"


def test_request_id_resets_to_dash_outside_request():
    # After the request completes the context var must be cleared so background
    # work does not inherit a stale correlation id.
    _client().get("/ok")
    assert current_request_id() == "-"
