# ALPHA RADAR SIGNALS V3.1 — Sprint 9 Report
## Public Landing Page

**Date:** 2026-05-30  
**Sprint:** 9 — Public Landing Page  
**Status:** ✅ COMPLETE — 16/16 requirement checks pass

---

## Files Changed

| File | Change |
|------|--------|
| `app/dashboard/server.py` | 9 targeted edits to `_get_stats()`, `_PUBLIC_HTML`, and `index()` |

No scanner logic modified.

---

## Changes by Section

### 1. Hero

**Before:** CTA buttons: Join Telegram (if configured), Join Discord (if configured)  
**After:** CTA buttons: Join Telegram · Join Discord (if configured) + **View Live Signals** (always present)

The "View Live Signals" button (`href="/signals"`) appears unconditionally, ensuring the CTA works even when community URLs are not configured in `.env`.

### 2. Live Statistics — 4 KPI Cards

| Card | Before | After | Data Source |
|------|--------|-------|-------------|
| Card 1 | WIN RATE (7D) | **TOTAL SIGNALS** | `/api/public/performance` → `total_signals` |
| Card 2 | SIGNALS (7D) | **WIN RATE** | `/api/public/performance` → `win_rate` |
| Card 3 | AVG PNL | **PROFIT FACTOR** | `/api/public/performance` → `profit_factor` |
| Card 4 | UNIVERSE | **ACTIVE SIGNALS** | `/api/public/stats` → `open_signals` |

`profit_factor: null` renders as `∞` (no losses in history).

### 3. Latest Signals

- Limited to **10 signals** (was 15)
- Legacy 5m signals already excluded (MTF filter applied in `_get_stats()` since Sprint 1)
- **Each symbol now links to `/signal/{id}`** — `id` field added to `_row()` helper

### 4. Performance Section

**Before:** 3-column grid — Win Rate · Avg PnL · Open Now  
**After:** 4-column grid — Win Rate · **Profit Factor** · Avg PnL · Open Now

Data source changed: performance section now populated from **`/api/public/performance`** (via `loadPerf()`) instead of the 7-day stats endpoint. This ensures all-time accuracy.

### 5. Telegram / Discord

Unchanged — buttons injected from `.env` values:
- `TELEGRAM_CHANNEL_URL`
- `DISCORD_URL`

No links are hard-coded.

### 6. Affiliate Section

Unchanged — cards injected from `.env` values:
- `BINANCE_AFFILIATE_URL`
- `BYBIT_AFFILIATE_URL`
- `OKX_AFFILIATE_URL`
- `BITGET_AFFILIATE_URL` (bonus)

Section only renders if at least one URL is configured.

### 7. Donate Section

Unchanged — addresses injected from `.env` values:
- `DONATE_USDT_TRC20` (USDT TRC20)
- `DONATE_USDT_BEP20` (USDT BEP20)
- `DONATE_BTC` (Bitcoin)
- `DONATE_ETH` (Ethereum ERC20)

Section only renders if at least one address is configured.

### 8. FAQ — 5 Required Questions

Replaced 8 generic questions with the 5 specified:

| # | Question |
|---|---------|
| 1 | **What is Alpha Radar?** — Explains the service, 4-layer pipeline, Binance futures, free Telegram delivery |
| 2 | **Is this financial advice?** — Clear disclaimer: educational only, no investment advice |
| 3 | **Does the bot trade automatically?** — No. No exchange API access. No real funds touched |
| 4 | **How are signals generated?** — Step-by-step: 1D Trend → 4H Structure → 1H Setup → 15M Entry |
| 5 | **What timeframes are used?** — 1D / 4H / 1H / 15M; 5m excluded from all reports |

### 9. SEO — Open Graph + Twitter Card Tags

Added to `<head>`:

```html
<meta property="og:title"       content="ALPHA RADAR SIGNALS — Free AI Crypto Futures Signals"/>
<meta property="og:description" content="Free AI-powered crypto futures signals. Multi-timeframe analysis. Real-time results. No subscription required."/>
<meta property="og:type"        content="website"/>
<meta property="og:site_name"   content="ALPHA RADAR SIGNALS"/>
<meta name="twitter:card"        content="summary"/>
<meta name="twitter:title"       content="ALPHA RADAR SIGNALS — Free AI Crypto Futures Signals"/>
<meta name="twitter:description" content="Free AI-powered crypto futures signals. No subscription required."/>
```

Existing `<title>` and `<meta name="description">` preserved and updated.

---

## JS Architecture

Two fetch functions run on page load:

| Function | Endpoint | Frequency | Sets |
|----------|----------|-----------|------|
| `loadStats()` | `/api/public/stats` | every 6s | `s-wr`, `s-active`, `ps-wr`, `ps-open`, signal tables, leaderboard |
| `loadPerf()` | `/api/public/performance` | every 30s | `s-total`, `s-wr`, `s-pf`, `ps-pf`, `ps-pnl`, `ps-w`, `ps-l` |
| `loadPrices()` | `/api/public/prices` | every 3s | market price cards + bias row |

`loadPerf()` fires on load and every 30s (performance data is slower-changing than live prices).

---

## Validation

### Syntax
```
app/dashboard/server.py   ✅ OK
```

### Docker Build
```
docker compose build   →   ✅ SUCCESS
```

### docker compose up -d
```
signals-postgres   ✅ Healthy
signals-redis      ✅ Healthy
signals-bot        ✅ Started — no errors
```

### Page Validation (16 / 16)
```
✅ OG title tag
✅ OG description
✅ OG type
✅ Twitter card
✅ Total Signals card (id=s-total)
✅ Active Signals card (id=s-active)
✅ Profit Factor card (id=s-pf)
✅ View Live Signals button (href=/signals)
✅ Performance 4-column with Profit Factor (id=ps-pf)
✅ FAQ Q1 — What is Alpha Radar
✅ FAQ Q2 — financial advice
✅ FAQ Q3 — trade automatically
✅ FAQ Q4 — How are signals generated
✅ FAQ Q5 — What timeframes
✅ Signal link (href=/signal/)
✅ loadPerf() function
```

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 9 Public Landing Page*  
*Generated 2026-05-30*
