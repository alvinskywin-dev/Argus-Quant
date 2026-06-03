# Sprint 12–15 Retention Roadmap Report

**Date:** 2026-05-30  
**Branch:** develop  
**Status:** COMPLETE — all routes validated, zero backend errors

---

## Summary

Implemented 4 retention features in a single server.py addition:
- Sprint 12: Performance Analytics Center
- Sprint 13: Daily Market Radar
- Sprint 14: Setup Library
- Sprint 15: User Watchlist Foundation

---

## Files Changed

| File | Change |
|------|--------|
| `app/dashboard/server.py` | +1047 lines, -9 lines |

One file only. No new files, no DB migrations, no schema changes.

---

## Pages Added

| Route | Title | Description |
|-------|-------|-------------|
| `/performance-center` | Performance Analytics Center | Multi-period analytics: 24h/7D/30D, bands, pairs, distribution |
| `/market-radar` | Market Radar | Daily bias, risk, sentiment, strongest setups, sector radar |
| `/setup-library` | Setup Library | 8 educational setup cards (no private source code exposed) |
| `/watchlist` | Watchlist | localStorage-based symbol tracking, no login required |

---

## APIs Added

| Endpoint | Description |
|----------|-------------|
| `GET /api/public/performance-center` | Period analytics + confidence bands + pair leaderboard |
| `GET /api/public/market-radar` | Market bias, risk, sentiment, setups, sectors |
| `GET /api/public/setup-library` | Static educational setup library (8 setups) |
| `GET /api/public/watchlist?symbols=...` | Latest signal + price for up to 20 symbols |

---

## Navigation Updates

**Landing page (`_PUBLIC_HTML`) navbar:**
```
Signals | Market Radar | Performance | Setup Library | Watchlist | FAQ | Join Telegram
```

**Inner pages (`_page_shell`) navbar:**
```
Signals | Market Radar | Performance | Setup Library | Watchlist | FAQ
```

**Landing page footer links:** updated to match new nav.

---

## Landing Page Additions

1. **Today's Market Radar mini-section** — 5 cards (BTC bias, ETH bias, Market Risk, Sentiment, Signals 24H) with "View Full Market Radar →" link. Inserted between Telegram CTA and Strategy Engine sections.

2. **Performance Center CTA** — "📊 View Full Performance Analytics →" button added inside the existing performance section. Does NOT replace the existing section.

3. **Watchlist CTA** — "⭐ Track Favorite Coins →" link added beside "View All Signals →" in the signals section.

---

## SQL / Database Changes

**None.** All queries use existing `Signal`, `FundingRateSnapshot` tables via existing filters:
- `strategy = MTF_SMC_STRICT`
- `timeframe IN (15m, 1h, 4h, 1d)`

No new indexes added (existing indexes on `symbol`, `created_at`, `status` cover all new queries).

---

## Performance Impact

| Feature | Impact |
|---------|--------|
| `/api/public/performance-center` | Single 30D query, cached 45 seconds in memory |
| `/api/public/market-radar` | Two queries (signals 24H + funding latest), cached 45 seconds |
| `/api/public/setup-library` | Static data, no DB query |
| `/api/public/watchlist` | N queries (one per symbol, max 20), no cache (user-specific) |
| Landing page radar mini | Shares market-radar cache, +1 fetch per 45 second cycle |

Memory cache (`_api_cache` dict) uses ~5 KB at most. No background jobs added. No heavy SQL.

---

## Sample Size Gate (Sprint 12)

The Performance Analytics Center enforces:
- If `sample_size < 30` (closed signals in 30D): API returns `"data_collecting": true`
- Frontend hides all statistics and shows "Collecting Verified Performance Data" message
- Current DB state: 15 closed signals → "Collecting" mode is active (correct behavior)

---

## Watchlist Design (Sprint 15)

- No login required
- Data stored in browser `localStorage` key `alpha_radar_watchlist`
- Max 20 symbols
- API returns: latest signal (or null), current price from WebSocket cache, status
- "Notify me on Telegram" CTA links to `/faq`
- Backend: `GET /api/public/watchlist?symbols=BTCUSDT,ETHUSDT,...`

---

## Setup Library (Sprint 14)

8 educational setups implemented:
1. Trend Continuation — Active
2. Pullback Entry — Active
3. BOS Retest — Active
4. Liquidity Sweep — Active
5. Funding Reversal — Active
6. Order Block Retest — Active
7. FVG Retest — Active
8. VWAP Reclaim — Active

Each card contains: name, description, required conditions, invalidation, example, risk notes, status. No private source code exposed — all content is trading-concept level.

---

## Validation Results

```
curl -s http://127.0.0.1:8010/performance-center | head     → ✅ 200 HTML
curl -s http://127.0.0.1:8010/market-radar | head           → ✅ 200 HTML
curl -s http://127.0.0.1:8010/setup-library | head          → ✅ 200 HTML
curl -s http://127.0.0.1:8010/watchlist | head              → ✅ 200 HTML

curl -s http://127.0.0.1:8010/api/public/performance-center → ✅ Valid JSON
curl -s http://127.0.0.1:8010/api/public/market-radar       → ✅ Valid JSON
curl -s http://127.0.0.1:8010/api/public/setup-library      → ✅ Valid JSON (8 setups)
curl -s "http://127.0.0.1:8010/api/public/watchlist?symbols=BTCUSDT,ETHUSDT,SOLUSDT" → ✅ Valid JSON

docker logs signals-bot --tail=300 | grep -Ei "error|traceback|exception" → ✅ Zero errors
```

---

## What Was Not Changed

- Signal generation logic — untouched
- `.env` thresholds — untouched
- Telegram bot broadcasting — untouched
- V7 landing page hero — untouched
- Existing `/performance` page — untouched (new `/performance-center` is the expanded version)
- Existing performance section on landing page — not replaced, CTA added alongside it
- Affiliate / donate sections — untouched
- Production scanner — untouched

---

## Remaining Recommendations

1. **Watchlist Telegram notifications** — when user clicks "Notify me on Telegram", link to a Telegram bot `/watch BTCUSDT` command (future sprint)
2. **Market Radar top movers** — integrate real 24H price change data from Binance REST `/fapi/v1/ticker/24hr` (currently not fetched; would add a lightweight cron fetch)
3. **Performance Center sharing** — add a "Share this report" link that generates a short URL
4. **Setup Library "Learn why" link** — wire the "signal row" analysis modal to match setup names from the library
5. **Performance Center — add EXPIRED to closed sample** — currently EXPIRED signals reduce the sample count but users may want to configure this behavior
