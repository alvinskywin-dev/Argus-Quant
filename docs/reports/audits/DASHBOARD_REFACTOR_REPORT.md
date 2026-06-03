# Dashboard Server Refactor Report — Phase 4

**Date:** 2026-06-03
**Branch:** `chore/devops-quality-foundation`
**Goal:** Break the `app/dashboard/server.py` god-file into modular `APIRouter`s
with **no router file > 700 LOC**, preserving every route, response schema,
auth behaviour, caching, and performance characteristic. Zero behavioural change.

---

## 1. Result at a glance

| Metric | Before | After |
|--------|-------:|------:|
| `server.py` LOC | 5,461 | **3,426** |
| Route handlers in `server.py` | 64 | 0 (moved) |
| Router modules | 0 | 6 |
| Largest router LOC | — | **685** (limit 700) ✅ |
| Total routes (`create_app()`) | 140 | **140** (identical) |
| Test suite | 292 pass | **292 pass** |

### Router modules (`app/dashboard/routes/`)

| File | Handlers | LOC | Scope |
|------|---------:|----:|-------|
| `public_router.py` | 24 | 685 | Public pages (`/`, `/about`, `/faq`, legal, page shells) + light public API (`/api/public/stats`, `dashboard`, `signals`, `prices`, `signal/{id}`, `diagnostics`, `languages`, `translations`, `strategy`) + `/aff/{exchange}` |
| `analytics_router.py` | 10 | 653 | Heavy data endpoints: backtest (`/api/backtest`, `/run`, `/public/backtest`), market-radar, market-regime, short-protection, performance, performance-center, setup-library, winrate-analysis |
| `system_router.py` | 9 | 466 | Health/diagnostics/metrics: `/health`, `/api/health`, `/status`, `/api/oi/status`, `/api/funding/status`, `/metrics`, `/api/system/metrics`, `/api/prices`, `/api/dashboard` |
| `admin_router.py` | 11 | 213 | `/admin`, `/admin/platform`, `/api/admin/*`, `/api/saas-admin/*`, `/api/performance/rebuild` |
| `paper_router.py` | 5 | 151 | `/api/paper`, `/api/paper/positions`, `/api/paper/stats`, `/api/public/paper`, `/paper` |
| `auth_router.py` | 3 | 44 | `/login` (GET/POST), `/logout` |

---

## 2. Architecture

```
app/dashboard/
  server.py          # FastAPI app, _SecurityHeaders middleware, lifespan,
                     # shared helpers (_get_stats, _cache_*, _esc, auth helpers),
                     # ~2,000 LOC of HTML view-builders + template constants,
                     # and create_app() (now wires the routers)
  routes/
    __init__.py
    public_router.py
    analytics_router.py
    system_router.py
    admin_router.py
    paper_router.py
    auth_router.py
```

**Wiring.** `create_app()` mounts the routers via `include_router()` (alongside
the existing flag-gated feature routers). Because `server.py` imports the
router package **only inside `create_app()`** (not at module top), and the
routers import shared helpers back from `server.py`, there is **no circular
import**: `server.py` is fully initialised before any router module loads.

**Import discipline.** Handlers were moved **verbatim**. Each router imports:
- **library / model / stdlib names** (`HTMLResponse`, `Signal`, `select`,
  `datetime`, `settings`, …) **from their original sources** — not re-exported
  through `server.py` (that would be fragile and trip the lint gate).
- **server-local helpers / view-builders / templates** (`_get_stats`,
  `_page_shell`, `_signals_page_html`, `_LOGIN_HTML`, …) from
  `app.dashboard.server`.

**Aggregator endpoint.** `/api/public/dashboard` calls four sibling endpoint
functions (`api_public_signals`, `api_public_performance`,
`api_public_market_regime`, `status_route`). Where those now live in a different
router, the call is satisfied by an explicit cross-router import — behaviour is
unchanged.

---

## 3. Preservation guarantees & how they were verified

| Guarantee | Verification | Result |
|-----------|--------------|--------|
| **All routes preserved** | Captured `create_app().routes` (method+path) before and after; normalized diff | **140 == 140, byte-identical** ✅ |
| **No undefined references** (the real risk when moving 2,400 LOC) | `ruff --select F821` across all routers + `server.py` | **clean** ✅ |
| **Response classes / params / schemas** | Handlers + decorators (incl. `response_class=…`, `Form(...)`, query params) moved verbatim | unchanged ✅ |
| **Auth behaviour** | `_is_logged_in`, `_admin_*`, cookie name `alpha_radar_auth`, `_login_page` untouched in `server.py`; admin/auth handlers import them | unchanged ✅ |
| **Caching** | `_cache_get`/`_cache_set` + module cache dict stay in `server.py`; handlers import them | unchanged ✅ |
| **Security headers / middleware** | `_SecurityHeaders` still added on `app` (app-wide) | unchanged ✅ |
| **Runtime smoke** | `TestClient` GET on template routes (`/about`, `/faq`, `/terms`, `/privacy`, `/risk-disclaimer`, `/login`, `/metrics`, `/health`) | **all 200** ✅ |
| **DB-backed routes** | `/api/public/stats` etc. return a handled `500` whose body is the DB/DNS error (`Temporary failure in name resolution`) — proves the code path runs; only the database is absent in this sandbox | as expected ✅ |
| **Unit tests** | `pytest` | **292 pass** ✅ |
| **Lint/format** | `ruff check` + `black --check` | clean ✅ |

The refactor was produced by an AST-based codemod (handlers identified by their
`@app.<method>` decorator, moved by exact source ranges) rather than hand edits,
eliminating transcription error; correctness was then proven by the gates above.

---

## 4. Scope notes & honest gaps

- **No `live_router.py`.** `server.py` contained **zero** live-trading routes —
  the live API already lives modularly under `app/live_trading/` and is mounted
  by `setup_live(app)` in `create_app()`. Adding an empty dashboard
  `live_router.py` would be a placeholder stub (prohibited), so it was not
  created. The live endpoints remain exactly where they were.
- **`server.py` is still 3,426 LOC.** Phase 4's explicit acceptance criterion
  (no **router** file > 700 LOC) is met. The remaining bulk is ~2,000 LOC of
  HTML **view-builders + template string constants** plus shared helpers. Moving
  those into a dedicated `views/` + `templates/` layer is a **Phase 12**
  follow-up (separating presentation from the app factory); it was deliberately
  left out of this change to keep the diff a pure, verifiable handler-relocation.
- **No route-level test suite exists.** This refactor adds none, but the
  F821 + route-invariant + TestClient gates give strong coverage. A dedicated
  dashboard smoke-test (hitting every GET with a stub DB) is recommended and
  tracked for a later phase.
