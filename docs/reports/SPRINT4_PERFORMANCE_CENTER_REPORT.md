# ALPHA RADAR SIGNALS V3.1 — Sprint 4 Report
## Performance Center

**Date:** 2026-05-30  
**Sprint:** 4 — Performance Center  
**Status:** ✅ COMPLETE

---

## Files Changed

| File | Change |
|------|--------|
| `app/dashboard/server.py` | `api_public_performance()` — full rewrite; `_performance_page_html()` — full rewrite; `_sqlfunc` added to SQLAlchemy imports |

No other files modified.

---

## Route

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/performance` | Public | Performance Center HTML page |
| `GET` | `/api/public/performance` | Public | JSON metrics endpoint |

---

## API Endpoint — `GET /api/public/performance`

### Filter Applied

```python
Signal.strategy == "MTF_SMC_STRICT"
Signal.timeframe.in_(["15m", "1h", "4h", "1d"])
```

Legacy 5m signals, archived signals, and any other strategy are excluded.  
No row cap — full signal history is scanned.

### Response Schema (all 16 required fields present)

```json
{
  "total_signals":         29,
  "closed_signals":        1,
  "open_signals":          28,
  "win_rate":              0.0,
  "loss_rate":             100.0,
  "avg_pnl":               -4.73,
  "total_pnl":             -4.73,
  "avg_rr":                2.2,
  "profit_factor":         0.0,
  "avg_hold_time_minutes": 9.0,
  "long": {
    "total": 0, "wins": 0, "losses": 0,
    "win_rate": 0.0, "avg_pnl": 0.0, "avg_rr": 0.0
  },
  "short": {
    "total": 1, "wins": 0, "losses": 1,
    "win_rate": 0.0, "avg_pnl": -4.73, "avg_rr": 2.2
  },
  "best_symbols":  [{"symbol":"...", "avg":..., "count":...}],
  "worst_symbols": [{"symbol":"...", "avg":..., "count":...}],
  "symbol_leaderboard": [
    {
      "symbol": "OPGUSDT", "total": 1, "wins": 0, "losses": 1,
      "win_rate": 0.0, "avg_pnl": -4.73, "total_pnl": -4.73,
      "avg_rr": 2.2,
      "long":  {"total": 0, "wins": 0, "avg_pnl": 0.0},
      "short": {"total": 1, "wins": 0, "avg_pnl": -4.73}
    }
  ],
  "monthly": [
    {
      "month": "2026-05", "signals": 1, "wins": 0, "losses": 1,
      "win_rate": 0.0, "total_pnl": -4.73, "profit_factor": 0.0
    }
  ]
}
```

### Edge Cases Handled

| Condition | Behaviour |
|-----------|-----------|
| No closed signals | Returns all zero values; page shows "Not enough closed trades yet" |
| `profit_factor` denominator = 0 (no losses) | Returns `null` → rendered as `∞` in UI |
| `profit_factor` numerator = 0 (no wins) | Returns `0.0` (correct: no profit generated) |
| `avg_hold_time_minutes` — signal missing `closed_at` | Signal excluded from hold-time calculation |
| `avg_hold_time_minutes` — no closed signals with times | Returns `null` → rendered as `—` in UI |

### Backward-Compatibility Aliases

Old consumers of the endpoint are unaffected. The response also includes:
`total_closed`, `wins`, `losses`, `avg_hold_min`, `leaderboard` (top 10, legacy shape)

---

## Metrics Implemented

| Metric | Source |
|--------|--------|
| Total Signals | `closed_signals + open_signals` |
| Closed Signals | COUNT WHERE status IN (TP1/TP2/TP3/SL) |
| Open Signals | COUNT WHERE status = OPEN |
| Win Rate | wins / closed × 100 |
| Loss Rate | losses / closed × 100 |
| Average PnL | mean(pnl_pct) of closed |
| Total PnL | sum(pnl_pct) of closed |
| Average RR | mean(risk_reward) of closed |
| Profit Factor | gross_wins / gross_losses; `null` when denominator = 0 |
| Avg Hold Time (min) | mean(closed_at − created_at) for signals with both timestamps |

### LONG / SHORT Breakdown

Both `long` and `short` dicts contain: `total`, `wins`, `losses`, `win_rate`, `avg_pnl`, `avg_rr`

### Symbol Leaderboard

Per-symbol: `symbol`, `total`, `wins`, `losses`, `win_rate`, `avg_pnl`, `total_pnl`, `avg_rr`  
Plus nested `long` / `short` breakdown per symbol for client-side filtering.  
Sorted by `total_pnl` descending (best performers first).

### Monthly Breakdown

Per month: `month`, `signals`, `wins`, `losses`, `win_rate`, `total_pnl`, `profit_factor`

---

## UI — Performance Center Page (`/performance`)

### Sections

1. **Header** — "ALPHA RADAR PERFORMANCE CENTER" / "MTF ENGINE ONLY · strategy = MTF_SMC_STRICT · 15m / 1H / 4H / 1D"

2. **Warning Banner** — Yellow-bordered: "⚠ Legacy 5m signals are excluded from this report."

3. **6 KPI Cards** (responsive grid, collapses on mobile):
   - Total Signals (with open count sub-label)
   - Win Rate (green/red by threshold ≥50%) + loss rate sub-label
   - Profit Factor (teal; shows `∞` when null)
   - Avg PnL / Trade (green/red)
   - Avg RR (yellow)
   - Avg Hold Time (auto-formats: `Xh Ym`)

4. **Empty State** — Shows "Not enough closed trades yet" when `closed_signals = 0`

5. **LONG / SHORT Cards** (side-by-side, collapses on mobile):  
   Each shows: Total · Wins · Losses · Win Rate · Avg PnL · Avg RR

6. **Symbol Leaderboard** (full-detail table):
   - Columns: Symbol · Total · Wins · Losses · Win Rate · Avg PnL · Total PnL · Avg RR
   - Filter buttons: **All** / **LONG** / **SHORT** — client-side, no reload
   - When LONG/SHORT filter active: shows per-side stats; Total PnL and Avg RR show `—`

7. **Monthly Performance Table**:
   - Columns: Month · Signals · Wins · Losses · Win Rate · Total PnL · Profit Factor
   - Ordered newest first
   - Profit Factor shows `∞` when no losses in that month

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

### API Smoke Test
```
curl http://127.0.0.1:8010/api/public/performance
→ 200 OK
→ 16 / 16 required fields present
→ symbol_leaderboard keys: symbol, total, wins, losses, win_rate, avg_pnl, total_pnl, avg_rr, long, short
→ monthly keys: month, signals, wins, losses, win_rate, total_pnl, profit_factor
```

### Page Test
```
GET /performance   →   200 OK
```

---

## Known Limitations

1. **Limited history**: The live database has 29 signals (1 closed, 28 open). Metrics are based on a very small sample — performance statistics will become meaningful as more signals close.

2. **No historical backfill**: The `avg_hold_time_minutes` calculation requires `closed_at` to be set by the tracker. Old signals closed before the `closed_at` column was fully propagated will be excluded from hold-time calculation.

3. **`profit_factor = 0.0` vs `null`**: When all closed signals are losses (no wins), `profit_factor` returns `0.0`. This is mathematically correct (no gross profit / gross loss). The UI displays `0.0`. Only `null` (denominator = 0, meaning no losses at all) triggers the `∞` display.

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 4 Performance Center*  
*Generated 2026-05-30*
