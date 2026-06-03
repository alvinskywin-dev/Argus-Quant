"""Phase 9 — security headers, incl. the Content-Security-Policy.

The CSP must keep allowing exactly the external origins the UI loads (Chart.js,
the QR widget, Google Fonts); dropping one silently breaks the dashboard, so we
pin them here. It must also keep the hardening directives that lock down
plugins, base-uri, framing and form posting.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.dashboard.server import CONTENT_SECURITY_POLICY, _SecurityHeaders


def _client():
    app = Starlette(routes=[Route("/x", lambda r: PlainTextResponse("ok"))])
    app.add_middleware(_SecurityHeaders)
    return TestClient(app)


def test_core_security_headers_present():
    h = _client().get("/x").headers
    assert h["X-Frame-Options"] == "DENY"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert h["Permissions-Policy"]
    assert h["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_csp_allows_every_origin_the_ui_loads():
    csp = CONTENT_SECURITY_POLICY
    # Scripts: Chart.js + QR widget.
    assert "https://cdn.jsdelivr.net" in csp
    assert "https://cdnjs.cloudflare.com" in csp
    # Google Fonts: stylesheet host + font-file host.
    assert "https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in csp
    # Inline server-rendered blocks/handlers require unsafe-inline.
    assert "'unsafe-inline'" in csp
    # QR codes render to data: URLs; Google profile avatars (P11) come from
    # the googleusercontent CDN.
    assert "img-src 'self' data: https://*.googleusercontent.com" in csp


def test_csp_keeps_hardening_directives():
    for directive in (
        "default-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in CONTENT_SECURITY_POLICY
