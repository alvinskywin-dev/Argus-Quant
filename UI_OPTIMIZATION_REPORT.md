# Alpha Radar Signals — V12 SaaS UI Optimization Report

**Scope:** Front-end only. The `/app` vanilla-JS SPA was made faster, denser, and more
premium without touching backend logic, the trading engine, API contracts, live-trading
gates, or framework choice. No real trading was enabled and no secrets are exposed.

**Files changed (3):**

| File | Change |
| --- | --- |
| `app/dashboard/static/saas/saas.css` | Layout/typography/density/responsive overhaul, new components |
| `app/dashboard/static/saas/saas.js` | Dashboard feed, analytics, page polish, micro-UX, perf/caching/chart lifecycle |
| `app/dashboard/saas_app.py` | Asset cache-buster bumped `v=12 → v=13` (so clients fetch the new assets) |

No backend modules, routes, schemas, or feature gates were modified.

---

## Before — problems observed

- Too much empty horizontal space on large screens; content drifted right and unbounded.
- Dashboard cards felt small relative to 1920px screens; pages looked sparse.
- Typography too small; muted labels hard to read; weak hierarchy/contrast.
- Tables usable but not premium; charts oversized and half-empty.
- Dashboard "Recent Signals" only showed confidence buckets, not actual signal rows.
- Native `confirm()` dialogs, no button loading states, no page transitions.
- Charts were never destroyed before re-creation (Chart.js leak risk); public data was
  re-fetched on every route change; no per-page refresh isolation.

---

## Fixes applied (by phase)

**Phase 1 — Layout.** `--maxw:1440px` centered main content (`margin:auto`, `padding:24px`);
sidebar narrowed `248px → 236px`; topbar made more compact; consistent `16px` grid gaps
via `--gap`; KPI grid stays 4-up on wide screens so the dashboard fills 1920px cleanly.

**Phase 2 — Typography.** Base size up (`14px`, line-height `1.5`); KPI value `27px → 30px`;
table cells `13px → 13.5px` with stronger `td b` contrast; brighter `--txt`/`--muted`
tokens; sticky, higher-contrast table headers.

**Phase 3 — Dashboard density.** Sections now: KPI cards → Market Regime → System Health
→ **Live Signals** → Winrate Summary. KPIs are compact-but-stronger and user-centric for
all roles (platform KPIs only added for admins, removing the "—" sparseness). Added a real
**Live Signals feed** (Symbol / Side / TF / Confidence / RR / Status / PnL) sourced from the
existing `/api/public/signals` endpoint, with a 20s active-page refresh. The confidence-bucket
table was **removed from the dashboard and moved to Analytics only**.

**Phase 4 — Paper Trading.** Open positions render as premium, responsive **position cards**
(`.pcards`, auto-fill grid → works on mobile/tablet) with clear PnL coloring; reset now uses a
polished confirmation modal; added a **"No trade history yet"** empty state.

**Phase 5 — Analytics.** Compact **summary cards** above the charts (Sample size, Long winrate,
Short winrate, Best confidence, Best RR). Charts moved to a **2×2 responsive grid**, each in a
`.chart-box` constrained to **300px** (`maintainAspectRatio:false`) so panels are no longer huge
and empty. Added a 4th chart (Signals per Confidence) and the confidence-bucket table.

**Phase 6 — Exchange Vault.** Status badges with icons: **✓ Connected**, **○ Not Connected**,
**⟳ Test Required**, **🛡 MOCK Safe**. Connect modal widened (`.modal.wide`); warning copy
reworded to avoid bad wrapping; secrets remain encrypted and never displayed.

**Phase 7 — Auto Trading.** Status cards above config (Auto Enabled, Total Opened, Total
Skipped, Safety Status); config form uses a **2-column** layout (`.form-grid`); execution
history kept scannable with an explanatory empty state.

**Phase 8 — Safety Center.** Kill switches placed in a serious red **Danger Zone** panel; the
kill switch now uses a **confirmation modal** instead of `confirm()`; added a status legend
(**Green = trading allowed / Red = trading blocked**).

**Phase 9 — Live Trading.** MOCK-mode banner made very explicit about the gate requirements;
large mode badge; all three tabs (Positions/Orders/Trades) render explanatory empty states.

**Phase 10 — Profile.** Cleaner account card; sessions table with **partially masked IPs**
(`1.2.•••.•••`); added a **Security Recommendations** card (enable 2FA, never share API keys,
use keys without withdrawal permission).

**Phase 11 — Micro UX.** Page-transition fade on route change; button **loading states**
(`withLoading` + spinner); active-nav indicator bar; confirmation modals; richer empty/error
states; only the active page auto-refreshes (per-page timer registry).

**Phase 12 — Performance.** Added a 20s TTL cache for public/idempotent GETs (`cachedGet`,
dedupes duplicate calls across route changes); Chart.js instances are tracked and **destroyed
before re-creation**, and **skipped entirely when the data signature is unchanged** (`mkChart`);
per-page `setInterval` registry cleared on every route change (`clearPageTimers`) to prevent
leaks; hashchange routing debounced (30ms) with a route token to ignore stale renders;
charts/timers/cache are also torn down on logout.

**Phase 13 — Mobile.** Sidebar drawer + scrim under 860px; KPIs collapse to 1 column on phones;
tables scroll horizontally (`.t-wrap`); position cards replace dense tables on small screens;
header wraps cleanly. Breakpoints tuned at **1280 / 1024 / 860 / 520 / 390** (covering the
1920 / 1440 / 1024 / 768 / 414 / 375 targets).

---

## Performance improvements (summary)

- Public dashboard data cached 20s → fewer redundant network calls on navigation.
- Chart.js: explicit destroy-before-recreate + unchanged-data short-circuit → no leak, no flicker.
- Per-page interval isolation → background pages no longer poll.
- Debounced router + stale-render guard → no double-render on rapid hash changes.

---

## QA performed (Phase 14)

- `node --check app/dashboard/static/saas/saas.js` → **OK**
- `docker compose build bot` → **Built**
- `docker compose up -d` → `signals-bot` **healthy**
- `docker logs signals-bot --since=3m | grep -Ei "error|traceback|exception|..."` → **clean**
- Served shell references `saas.css?v=13` / `saas.js?v=13`; served JS byte-identical to source.
- Public endpoints (`/status`, `/api/public/winrate-analysis`, `/api/public/market-regime`,
  `/api/public/signals`, `/api/live/status`) → **200**.
- Auth-gated endpoints (`/api/paper`, `/api/exchange`, `/api/auto`, `/api/safety`) → **401**
  without a token (no data leak).
- Disabled modules still degrade to a "module disabled" card (404 → `disabledCard`).
- No real-trading calls added; live execution stays MOCK behind the existing gate.

---

## Remaining limitations

- Visual review was done via served-asset and endpoint verification, not an automated headless
  browser (none available in this environment); a manual pass across the six breakpoints in a
  real browser is still recommended.
- The Live Signals feed reflects whatever `/api/public/signals` returns; for fully-closed
  signals it shows realized `pnl_pct`, and for `OPEN` rows it shows a "live" placeholder rather
  than a continuously-updating mark price (the public endpoint does not stream live PnL).
- Cache TTL is a fixed 20s; not yet tuned per-endpoint.
- Exchange logo SVGs depend on assets under `/static/exchanges/`; missing files fall back to
  no icon (handled by `onerror`).
