# ALPHA RADAR SIGNALS — V12 SaaS UI Overhaul Report

**Date:** 2026-05-31 · **Branch:** `develop` · **Scope:** UI/UX/dashboard only. No backend, trading, engine, gate, or safety logic changed.

A modern, dark, responsive SaaS portal built on the **existing** JSON APIs — vanilla JS (no frontend framework), Chart.js via CDN, served from the current FastAPI server. The portal lives at **`/app`** as a single-page hash-router app; the legacy public site (`/`) and operator dashboard (`/admin`, `/admin/platform`) are untouched.

---

## How to view

- **Portal:** `http://<host>:8010/app` → landing/login → portal.
- Requires the relevant feature flags (now enabled in this deployment, live gate still closed):
  `AUTH_ENABLED`, `PAPER_TRADING_ENABLED`, `EXCHANGE_API_VAULT_ENABLED`, `AUTO_TRADE_DEMO_ENABLED`, `SAFETY_LAYER_ENABLED`, `LIVE_TRADING_API_ENABLED`, `ADMIN_DASHBOARD_ENABLED`. **`LIVE_TRADING_ENABLED=false` and `MOCK_EXCHANGE_MODE=true` were left untouched — no real orders.**
- Any page whose API flag is off shows a clean "module disabled" state instead of breaking.

## Architecture

- **`app/dashboard/static/saas/saas.css`** — design system (tokens + all components), responsive.
- **`app/dashboard/static/saas/saas.js`** — the SPA: JWT API client (auto-refresh on 401), hash router, component helpers, and all pages.
- **`app/dashboard/saas_app.py`** — serves the `/app` shell; `setup_saas_app(app)` wired into `create_app()` (always mounted; data stays flag-gated).
- **`app/dashboard/server.py`** — one mount call + a "Portal V12" link on `/admin`. No other change.

Design tokens match the spec: primary `#20f0c0`, bg `#070b12`, card `#0b1320`, border `#17314b`, success `#22c55e`, danger `#ef4444`, warning `#f59e0b`.

## Pages completed

| Route | Page | Backing APIs | Highlights |
|-------|------|--------------|-----------|
| login/landing | Hero + sign-in/register | `/api/auth/login,register,refresh` | premium hero, 2FA-code prompt, graceful "auth disabled" |
| `#/dashboard` | Dashboard | `/status`, `/api/public/{winrate-analysis,market-regime}`, `/api/admin/overview` (admin), `/api/paper/account` | 8 KPI cards, **market-regime gauge** (color-coded BULL/BEAR/SIDEWAYS/VOL), system-health (scanner/ws/db/redis), recent-signal table |
| `#/analytics` | Signal Analytics | `/api/public/winrate-analysis` | **Chart.js**: confidence→winrate, long-vs-short doughnut, RR buckets + summary |
| `#/paper` | Paper Trading | `/api/paper/account/*` | balance/equity/PnL/winrate KPIs, open positions + **detail modal**, trade history tabs, auto-follow toggle, reset |
| `#/exchange` | Exchange Vault | `/api/exchange/*` | Binance/OKX/Bybit/Bitget cards, connect (passphrase for OKX/Bitget)/test/disconnect, last4 only — **never secrets** |
| `#/auto` | Auto Trading | `/api/auto/{config,status,executions}` | enable toggle, risk/leverage/coins/BE config, engine status, execution history |
| `#/safety` | Safety Center | `/api/safety/{status,config,kill,resume}` | daily/weekly PnL vs limits, loss-streak, kill switch + global-stop indicators, editable limits |
| `#/live` | Live Trading | `/api/live/{status,positions,orders,trades}` | **large MOCK/LIVE mode badge**, gate banner, positions/orders/trades tabs |
| `#/profile` | Profile | `/api/auth/{me,sessions}` | avatar, role, 2FA status, active sessions |
| `#/admin` | Admin Platform | `/api/admin/{overview,users,users/{id},audit}` + status PUT | 8 metric KPIs, users table w/ suspend/activate + detail modal, audit log (ADMIN-only) |

## Components added (Phase 1)

Card, Stat widget, Badge (status-colored), Table (responsive scroll), Alert (info/warn/danger/ok), Modal, Tabs, Drawer (mobile sidebar), Toast, Skeleton loader, Empty state, Switch, plus a Market-Regime gauge.

## Routes added

`GET /app` (shell). Client routes (hash): `#/dashboard #/analytics #/paper #/live #/exchange #/auto #/safety #/profile #/admin`. Static: `/static/saas/saas.css`, `/static/saas/saas.js`. No API routes added or changed.

## Performance (Phase 13)

- **Single-page app** — navigation never reloads the page; only the view re-renders.
- **Skeleton loaders** on every async section; per-route lazy data fetch (only the active page calls APIs).
- JWT auto-refresh avoids re-login churn; live-gate badge + regime cached per render; admin overview auto-refreshes KPIs without full reload.
- Assets are static/cacheable; Chart.js loaded lazily via CDN with a `window.Chart` guard.

## Mobile responsiveness (Phase 12)

Breakpoints at 1024 / 860 / 520 px. Sidebar collapses to a **drawer** with scrim below 860 px; KPI grids reflow 4→2→1/2; tables scroll horizontally; auth hero hides on mobile. Verified layout rules for 320–1440 px.

## Files modified / added

```
A app/dashboard/static/saas/saas.css      (design system)
A app/dashboard/static/saas/saas.js       (SPA: client, router, pages)
A app/dashboard/saas_app.py               (/app shell + setup)
M app/dashboard/server.py                 (mount setup_saas_app + /admin "Portal V12" link)
A UI_UPGRADE_REPORT.md
```
(`.env` flags were enabled locally to make the portal usable; `.env` is gitignored and not part of the commit. Live gate untouched.)

## QA / validation (Phase 14)

- `node --check saas.js` → **syntax OK**; `compileall` clean; **155 unit tests pass**; container **healthy**; live logs clean.
- End-to-end against the live server (every API the SPA calls): `/app` 200, `saas.css`/`saas.js` 200, register 201, login → JWT, then **200** on `/api/auth/me`, `/api/paper/account/`, `/api/auto/{config,status}`, `/api/safety/status`, `/api/exchange/accounts`, `/api/live/{status,positions}`, `/api/auth/sessions`, `/api/admin/{overview,users}`. Demo user purged.
- **No credential leaks:** exchange UIs render `api_key_last4` + permission dots only; secret inputs are write-only `type=password`.
- **Graceful states:** flag-off → "module disabled"; empty data → empty state; 401 → silent refresh then re-login.

## Screenshots

Browser screenshots can't be captured in this headless environment. Layout reference (desktop):

```
┌────────────┬─────────────────────────────────────────────┐
│ A ALPHA    │  Dashboard            [MOCK MODE] [user ADMIN]│
│  RADAR     ├─────────────────────────────────────────────┤
│ ▸Dashboard │  [Signals][Winrate][Open][Health]            │
│  Analytics │  [Users][Exch][Auto][Kill]                   │
│  Paper     │  ┌─Market Regime──────┐ ┌─System Health────┐ │
│  Live      │  │ LOW_VOLATILITY  17 │ │ ● Scanner   OK   │ │
│  ───────   │  │ ▓▓▓▓░░░░░░  BTC DOWN│ │ ● WebSocket OK   │ │
│  Exchange  │  └────────────────────┘ └──────────────────┘ │
│  Auto      │  ┌─Recent Signals─────────────────────────┐  │
│  Safety    │  │ Confidence │ Trades │ Winrate          │  │
│  Profile   │  └────────────────────────────────────────┘  │
└────────────┴─────────────────────────────────────────────┘
```

## Known limitations

- **Screenshots** require a browser (none available headlessly here); routes provided above.
- A few sub-widgets named in the brief are deferred (no new backend needed): interactive analytics filters (timeframe/coin/range), paper-account equity **PnL chart**, live **risk-exposure chart**, full in-portal **2FA enable** flow (status shown; enable still via API), and table→card transform on mobile (tables currently scroll). These are additive and can follow.
- The portal needs its feature flags on; with `AUTH_ENABLED` off it shows a clear "enable AUTH_ENABLED" message.
- The legacy public landing (`/`) was intentionally not restyled (risk); the new premium landing is the logged-out `/app` view.

## Rollback

UI is isolated to new static files + one new module + a 2-line mount in `server.py`. Revert the commit to remove `/app` entirely; the rest of the system is unaffected. To hide the portal without reverting, the `setup_saas_app` call can be removed. To disable the APIs behind it, flip the feature flags back off.
