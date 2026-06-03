"""Router Decomposition — route-inventory regression lock.

server.py was decomposed into modular APIRouters (Phase 4). This test pins the
full public route surface so a future move/refactor cannot silently drop a
route, and smoke-checks the key endpoints the roadmap calls out (SPA at /app,
/admin/platform, health, metrics, live status). Set-based so adding routes is
fine; removing one fails.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import settings

# Every optional router flag — enable all so the inventory is the full surface.
_ROUTER_FLAGS = (
    "auth_enabled",
    "paper_trading_enabled",
    "exchange_api_vault_enabled",
    "auto_trade_demo_enabled",
    "safety_layer_enabled",
    "live_trading_api_enabled",
    "admin_dashboard_enabled",
    "reconciliation_enabled",
    "position_recovery_enabled",
    "order_failure_engine_enabled",
    "accounting_enabled",
)

# Baseline captured after the Phase 4 decomposition (+ P11/pilot routes). Every
# one of these MUST remain reachable; adding new routes is allowed.
BASELINE_ROUTES = frozenset(
    {
        "/",
        "/about",
        "/admin",
        "/admin/platform",
        "/aff/{exchange}",
        "/api/accounting/daily",
        "/api/accounting/summary",
        "/api/accounting/trades",
        "/api/accounting/user/{user_id}",
        "/api/admin/active-signals",
        "/api/admin/affiliate-stats",
        "/api/admin/audit",
        "/api/admin/overview",
        "/api/admin/safety/kill-all",
        "/api/admin/safety/resume-all",
        "/api/admin/safety/state",
        "/api/admin/users",
        "/api/admin/users/{user_id}",
        "/api/admin/users/{user_id}/status",
        "/api/auth/2fa/disable",
        "/api/auth/2fa/enable",
        "/api/auth/2fa/setup",
        "/api/auth/forgot-password",
        "/api/auth/google/callback",
        "/api/auth/google/login",
        "/api/auth/google/status",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/auth/refresh",
        "/api/auth/register",
        "/api/auth/reset-password",
        "/api/auth/sessions",
        "/api/auth/timezone",
        "/api/auth/timezones",
        "/api/auth/verify-email",
        "/api/auto/config",
        "/api/auto/executions",
        "/api/auto/status",
        "/api/backtest",
        "/api/backtest/run",
        "/api/dashboard",
        "/api/exchange/accounts",
        "/api/exchange/connect",
        "/api/exchange/disconnect",
        "/api/exchange/test",
        "/api/funding/status",
        "/api/health",
        "/api/live/balance",
        "/api/live/binance/preflight",
        "/api/live/close",
        "/api/live/exchanges",
        "/api/live/leverage",
        "/api/live/open",
        "/api/live/orders",
        "/api/live/pilot/config",
        "/api/live/pilot/emergency-close",
        "/api/live/pilot/open",
        "/api/live/pilot/preflight",
        "/api/live/positions",
        "/api/live/positions/{position_id}/emergency-close",
        "/api/live/status",
        "/api/live/trades",
        "/api/oi/status",
        "/api/order-failures",
        "/api/order-failures/list",
        "/api/order-failures/{failure_id}",
        "/api/order-failures/{failure_id}/mark-resolved",
        "/api/order-failures/{failure_id}/retry",
        "/api/paper",
        "/api/paper/account/",
        "/api/paper/account/auto-follow",
        "/api/paper/account/copy",
        "/api/paper/account/open",
        "/api/paper/account/orders",
        "/api/paper/account/positions",
        "/api/paper/account/positions/{position_id}/close",
        "/api/paper/account/reset",
        "/api/paper/account/simulate",
        "/api/paper/account/trades",
        "/api/paper/positions",
        "/api/paper/stats",
        "/api/performance/rebuild",
        "/api/prices",
        "/api/public/backtest",
        "/api/public/dashboard",
        "/api/public/diagnostics/{signal_id}",
        "/api/public/languages",
        "/api/public/market-radar",
        "/api/public/market-regime",
        "/api/public/paper",
        "/api/public/performance",
        "/api/public/performance-center",
        "/api/public/prices",
        "/api/public/setup-library",
        "/api/public/short-protection",
        "/api/public/signal/{signal_id}",
        "/api/public/signals",
        "/api/public/stats",
        "/api/public/strategy",
        "/api/public/translations",
        "/api/public/winrate-analysis",
        "/api/reconciliation/issues",
        "/api/reconciliation/run",
        "/api/reconciliation/status",
        "/api/recovery/run",
        "/api/recovery/status",
        "/api/saas-admin/audit",
        "/api/saas-admin/overview",
        "/api/saas-admin/safety-overview",
        "/api/saas-admin/users",
        "/api/saas-admin/users/{user_id}",
        "/api/saas-admin/users/{user_id}/status",
        "/api/safety/config",
        "/api/safety/kill",
        "/api/safety/resume",
        "/api/safety/status",
        "/api/system/metrics",
        "/app",
        "/backtest",
        "/faq",
        "/health",
        "/login",
        "/logout",
        "/market-radar",
        "/metrics",
        "/paper",
        "/performance",
        "/performance-center",
        "/privacy",
        "/risk-disclaimer",
        "/setup-library",
        "/signal/{signal_id}",
        "/signals",
        "/stats",
        "/status",
        "/terms",
    }
)


@pytest.fixture(scope="module")
def full_app():
    saved = {f: getattr(settings, f) for f in _ROUTER_FLAGS}
    for f in _ROUTER_FLAGS:
        setattr(settings, f, True)
    from app.dashboard import create_app

    app = create_app()
    yield app
    for f, v in saved.items():
        setattr(settings, f, v)


def _paths(app) -> set[str]:
    return {r.path for r in app.routes if getattr(r, "path", "")}


def test_all_baseline_routes_still_present(full_app):
    missing = BASELINE_ROUTES - _paths(full_app)
    assert not missing, f"routes disappeared after decomposition: {sorted(missing)}"


def test_route_count_not_regressed(full_app):
    # 142 baseline non-static paths; never fewer.
    assert len(_paths(full_app)) >= len(BASELINE_ROUTES)


def test_each_router_group_contributes(full_app):
    paths = _paths(full_app)
    for prefix in (
        "/api/public/",
        "/api/auth/",
        "/api/admin/",
        "/api/paper/",
        "/api/live/",
        "/api/safety/",
        "/api/system/",
    ):
        assert any(p.startswith(prefix) for p in paths), f"no routes under {prefix}"


# ── key-endpoint smoke checks (no DB / no auth needed) ────────────


@pytest.fixture(scope="module")
def client(full_app):
    return TestClient(full_app, follow_redirects=False)


def test_spa_served_at_app(client):
    r = client.get("/app")
    assert r.status_code == 200
    assert "saas" in r.text.lower() or "argus" in r.text.lower()


def test_admin_platform_route_responds(client):
    # Unauthenticated -> redirect to /login (route exists, not 404/500).
    r = client.get("/admin/platform")
    assert r.status_code in (200, 302)


def test_health_and_metrics(client):
    assert client.get("/health").status_code == 200
    m = client.get("/metrics")
    assert m.status_code == 200
    assert "alpha_radar_" in m.text


def test_live_status_json(client):
    r = client.get("/api/live/status")
    assert r.status_code == 200
    assert "mode" in r.json()
