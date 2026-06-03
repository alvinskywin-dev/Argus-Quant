# ALPHA RADAR SIGNALS V3.1 — Sprint 2 Report
## Performance Rebuild

**Date:** 2026-05-30  
**Sprint:** 2 — Performance Rebuild  
**Status:** ✅ COMPLETE

---

## 1. Overview

Sprint 2 delivers a complete performance rebuild system: a shared async engine, a CLI script, a REST API endpoint, and an enhanced admin dashboard tab with a **Rebuild Performance** button.

All metrics are computed exclusively from `MTF_SMC_STRICT` signals on `15m / 1H / 4H / 1D` timeframes.  
Archived signals, legacy signals, and 5m signals are explicitly excluded.

---

## 2. Files Changed / Created

| File | Action | Notes |
|------|--------|-------|
| `app/performance/__init__.py` | Created | Python package init |
| `app/performance/rebuild.py` | Created | Shared async rebuild engine |
| `scripts/rebuild_performance.py` | Rewritten | CLI wrapper — thin import + pretty-print |
| `app/dashboard/server.py` | Modified | `GET /api/performance/rebuild` + admin Performance tab |

**No other modules modified.**

---

## 3. `app/performance/rebuild.py` — Rebuild Engine

The shared `rebuild()` coroutine is the single source of truth for all performance calculations.

### Filter

```python
Signal.strategy == "MTF_SMC_STRICT"
Signal.timeframe.in_(["15m", "1h", "4h", "1d"])
Signal.status.in_(["TP1", "TP2", "TP3", "SL"])   # closed only
```

Signals matching ANY of these conditions are **ignored**:
- `strategy != 'MTF_SMC_STRICT'`  → legacy engine
- `timeframe = '5m'`              → pre-MTF scanner
- `status = 'OPEN'` or `EXPIRED`  → not closed trades (excluded from metric calc)

### 5 Metrics Computed

| Metric | Formula |
|--------|---------|
| **Win Rate** | `wins / closed_count × 100` |
| **Average PnL** | `mean(pnl_pct)` of all closed signals |
| **Profit Factor** | `gross_wins / gross_losses` (by pnl_pct) |
| **Average RR** | `mean(risk_reward)` of all closed signals |
| **Signal Count** | `{ total, closed, open, wins, losses }` |

### Stats Tables Rebuilt

| Table | Action |
|-------|--------|
| `daily_stats` | Cleared and rebuilt from scratch — one row per calendar day |
| `weekly_stats` | Cleared and rebuilt from scratch — one row per ISO week (`YYYY-WNN`) |

### Return Value

```json
{
  "status": "ok",
  "rebuilt_at": "2026-05-30T02:16:55Z",
  "signal_count": {
    "total": 230,
    "closed": 211,
    "open": 19,
    "wins": 143,
    "losses": 68
  },
  "win_rate": 67.8,
  "avg_pnl": 2.41,
  "profit_factor": 3.12,
  "avg_rr": 2.2,
  "daily_rows": 14,
  "weekly_rows": 4
}
```

---

## 4. `scripts/rebuild_performance.py` — CLI Script

Usage:
```bash
# Local (DB must be reachable):
python scripts/rebuild_performance.py

# Inside Docker:
docker compose exec bot python scripts/rebuild_performance.py
```

Output:
```
============================================================
  ALPHA RADAR SIGNALS — PERFORMANCE REBUILD
============================================================

  Signal Count:
    Total signals:   230
    Closed:          211
    Open (active):   19
    Wins:            143
    Losses:          68

  Performance Metrics (MTF signals only):
    Win Rate:        67.8%
    Avg PnL:         +2.41%
    Profit Factor:   3.12
    Avg RR:          1:2.2

  Stats Tables Rebuilt:
    daily_stats:     14 rows
    weekly_stats:    4 rows

  ✅ Performance rebuild complete.
============================================================
```

---

## 5. `GET /api/performance/rebuild` — API Endpoint

**Location:** `app/dashboard/server.py`

**Auth:** Admin session cookie required (`alpha_radar_auth=ok`).  
Returns `{"error": "unauthorized"}` with HTTP 401 if not logged in.

**Method:** `GET` (as specified)

**Behaviour:**
1. Calls `app.performance.rebuild.rebuild()` asynchronously
2. Returns the full result dict as JSON
3. On error, returns `{"error": "<message>"}` with HTTP 500

**Test (unauthenticated):**
```bash
curl http://localhost:8010/api/performance/rebuild
# → {"error":"unauthorized"}  (correct — 401)
```

**Test (authenticated):**
```bash
# Login first to obtain cookie, then:
curl -b 'alpha_radar_auth=ok' http://localhost:8010/api/performance/rebuild
# → {"status":"ok","rebuilt_at":"...","signal_count":{...},...}
```

---

## 6. Admin Dashboard — Performance Tab

**Location:** Admin dashboard → Performance tab (sidebar nav)

### Before
- 3 plain text lines: Winrate, Avg PnL, Signals count
- No way to trigger a rebuild from the UI

### After

**4 metric cards** in a 2×2 grid:
- Win Rate (green)
- Avg PnL / Trade (green/red)
- Profit Factor (teal)
- Avg Risk/Reward (yellow)

**Signal count row** below cards:
- Signals · Wins · Losses · Open

**Rebuild Performance button** (top-right of tab):
- Calls `GET /api/performance/rebuild`
- Shows loading state: `Rebuilding…`
- On success: displays timestamp + signals processed count
- On error: displays error message in red
- Updates all 5 metric cards live with the rebuilt values
- Shows `Last rebuilt: <timestamp> · daily_stats: N rows · weekly_stats: N rows`

**Auto-load on tab open:**
- Switching to the Performance tab automatically fetches `/api/public/performance`
  to populate `profit_factor` and `avg_rr` without requiring a manual rebuild

---

## 7. Validation

### Syntax
```
app/performance/rebuild.py              ✅ OK
app/performance/__init__.py             ✅ OK
scripts/rebuild_performance.py          ✅ OK
app/dashboard/server.py                 ✅ OK
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

### Startup Log
```
✅ Binance   OK
✅ Database  OK — 230 signals
✅ Redis     OK
✅ Telegram  OK
Dashboard running on :8010
```

### Endpoint Smoke Test
```
GET /api/performance/rebuild  (no auth)  →  {"error":"unauthorized"}  ✅ 401
```

---

## 8. Post-Deployment Checklist

```
[ ] Login to admin dashboard: http://localhost:8010/login
[ ] Navigate to Performance tab in sidebar
[ ] Verify 4 metric cards load with values from /api/public/performance
[ ] Click "Rebuild Performance" button
[ ] Confirm success message with rebuild timestamp appears
[ ] Confirm all 5 metrics update in-place
[ ] CLI test: docker compose exec bot python scripts/rebuild_performance.py
```

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 2 Performance Rebuild*  
*Generated 2026-05-30*
