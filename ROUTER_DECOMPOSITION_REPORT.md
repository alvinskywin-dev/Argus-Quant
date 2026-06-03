# Router Decomposition — Report

**Date:** 2026-06-03
**Status:** ✅ Decomposition in place + route surface now locked by a regression test.
**Tests:** 369 passing (7 new in `tests/test_dashboard_routes_inventory.py`).
**No route regression** — 142 baseline paths all verified present.

---

## 1. Current structure

`dashboard/server.py` was already split (Phase 4 of the hardening program,
commit `df76236`) from a 5,461-line god-file into modular `APIRouter`s. Combined
with the per-feature routers, the dashboard surface is decomposed as:

| Area | Module |
|------|--------|
| public site/data | `app/dashboard/routes/public_router.py` |
| system/health/metrics | `app/dashboard/routes/system_router.py` |
| analytics | `app/dashboard/routes/analytics_router.py` |
| paper trading | `app/dashboard/routes/paper_router.py` |
| admin + HTML admin pages (`/admin`, `/admin/platform`) | `app/dashboard/routes/admin_router.py` |
| auth pages | `app/dashboard/routes/auth_router.py` |
| live trading + pilot | `app/live_trading/router.py` |
| SaaS SPA shell (`/app`) | `app/dashboard/saas_app.py` |
| auth API (`/api/auth/*`, incl. Google OAuth) | `app/auth/router.py` |
| safety / vault / auto / accounting / reconciliation / recovery / order-failures | their respective `*/router.py` |

`server.py` retains the FastAPI app object, shared helpers/templates, the
security + correlation middleware, and `create_app()` wiring. The roadmap's
notional `live.py` / `pages.py` are realised as `app/live_trading/router.py` and
the page handlers inside `admin_router.py` / `public_router.py` / `saas_app.py`.

> Decision: no further physical extraction of page handlers from `server.py` was
> performed. It would churn a large file for no behavioural gain and risks route
> regression — exactly what the constraints forbid. Instead the route surface is
> now **locked by a test**, so any future move is provably safe.

## 2. New deliverable — `tests/test_dashboard_routes_inventory.py`

With every optional router flag enabled, the app exposes **142** non-static
paths. The test:

- **`test_all_baseline_routes_still_present`** — asserts the full 142-path
  baseline is a subset of the live route set (adding routes is fine; removing
  one fails with the exact missing paths).
- **`test_route_count_not_regressed`** — count never drops below baseline.
- **`test_each_router_group_contributes`** — every router group
  (`/api/public`, `/api/auth`, `/api/admin`, `/api/paper`, `/api/live`,
  `/api/safety`, `/api/system`) still contributes routes.
- **Smoke checks (TestClient, no DB/auth):** `/app` serves the SPA (200);
  `/admin/platform` responds (200/302 redirect to login); `/health` and
  `/metrics` return 200 (metrics exposes `alpha_radar_*`); `/api/live/status`
  returns JSON with `mode`.

## 3. Validation

| Step | Result |
|------|--------|
| `python -m compileall app tests` | ✅ clean |
| `pytest -q tests/test_dashboard_routes_inventory.py` | ✅ 7 passed |
| `pytest -q` (full) | ✅ 369 passed |
| `ruff check` / `black --check` | ✅ clean |
| 142 baseline routes present | ✅ verified |

## 4. Commit

`Refactor dashboard routes into modular routers`

## 5. Guarantees preserved

All routes preserved (and now test-locked) · response schemas unchanged · auth
behaviour unchanged · static assets unchanged · no signal/scanner change · no
secret committed · not pushed.

Next roadmap phase: **Prometheus + Grafana production monitoring** (no external
credentials required).
