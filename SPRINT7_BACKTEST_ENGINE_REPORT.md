# ALPHA RADAR SIGNALS V3.1 — Sprint 7 Report
## Backtest Engine

**Date:** 2026-05-30  
**Sprint:** 7 — Backtest Engine  
**Status:** ✅ COMPLETE

---

## Files Changed

| File | Change |
|------|--------|
| `app/dashboard/server.py` | `_compute_backtest()` helper extracted; `GET /api/backtest` added; `GET /api/public/backtest` updated to delegate; `_backtest_page_html()` rewritten |

No scanner logic changed.

---

## API Endpoint — `GET /api/backtest`

### Filter

```python
Signal.strategy == "MTF_SMC_STRICT"
Signal.timeframe.in_(["15m", "1h", "4h", "1d"])
Signal.status.in_(["TP1", "TP2", "TP3", "SL"])   # closed only
```

### Response Schema — all 12 required fields confirmed present

```json
{
  "total":            1,
  "wins":             0,
  "losses":           1,
  "win_rate":         0.0,
  "profit_factor":    0.0,
  "max_drawdown":     4.73,
  "sharpe_ratio":     0.0,
  "avg_rr":           2.2,
  "avg_pnl":          -4.73,
  "total_pnl":        -4.73,
  "rr_distribution":  [{"rr": "2.0", "count": 1}],
  "equity_curve":     [0.0, -4.73],
  "monthly": [
    {
      "month": "2026-05",
      "signals": 1,
      "wins": 0,
      "losses": 1,
      "win_rate": 0.0,
      "total_pnl": -4.73,
      "profit_factor": 0.0
    }
  ]
}
```

### Metric Definitions

| Metric | Formula |
|--------|---------|
| `win_rate` | wins / total × 100 |
| `profit_factor` | gross_wins / gross_losses; `0.0` when no wins; `null` when no losses (displays as `∞`) |
| `max_drawdown` | Peak-to-trough of cumulative PnL% curve |
| `sharpe_ratio` | mean(pnl) / stdev(pnl); 0.0 when ≤1 trade |
| `avg_rr` | mean(risk_reward) |
| `avg_pnl` | mean(pnl_pct) |
| `total_pnl` | sum(pnl_pct) |
| `equity_curve` | Cumulative PnL% at each trade; starts at 0.0 |
| `rr_distribution` | Count per 0.5-wide RR bucket, sorted ascending |

### Backward Compatibility

`GET /api/public/backtest` preserved as an alias — delegates to `_compute_backtest()`.  
`max_drawdown_pct` field preserved in response alongside `max_drawdown`.

---

## Page — `GET /backtest`

### Sections

1. **Header** — "BACKTEST ENGINE · MTF STRATEGY ONLY · strategy = MTF_SMC_STRICT · Closed signals"

2. **8 KPI Cards** (2×4 grid):
   - Total Trades (with W/L sub-label)
   - Win Rate (green ≥50%, red <50%)
   - Profit Factor (teal; `∞` when no losses)
   - Max Drawdown (red)
   - Sharpe Ratio (yellow)
   - Avg RR (yellow)
   - Avg PnL / Trade (green/red)
   - Total PnL (green/red)

3. **Equity Curve** — bar chart of cumulative PnL% per trade:
   - Green bars = cumulative value ≥ 0
   - Red bars = cumulative value < 0
   - Shows last 60 trades
   - Trade count label on right

4. **Summary Statistics** (left card) — full row breakdown of all 10 metrics

5. **RR Distribution** (right card) — horizontal gradient bars, one row per 0.5 RR bucket, showing trade count

6. **Monthly Results Table** — 7 columns:
   - Month · Signals · Wins · Losses · Win Rate · Total PnL · Profit Factor
   - Newest month first
   - Win rate colour-coded ≥50% green, <50% red
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

### curl Test
```
curl http://127.0.0.1:8010/api/backtest

Required keys: 12 / 12 present

  total               : 1
  wins                : 0
  losses              : 1
  win_rate            : 0.0
  profit_factor       : 0.0
  max_drawdown        : 4.73
  sharpe_ratio        : 0.0
  avg_rr              : 2.2
  avg_pnl             : -4.73
  rr_distribution     : 1 item
  equity_curve        : 2 points
  monthly             : 1 month

monthly[0] keys: month, signals, wins, losses, win_rate, total_pnl, profit_factor
```

### Page
```
GET /backtest   →   200 OK
```

---

## Architecture Note

`_compute_backtest(signals)` is a pure function (no DB calls) shared by both endpoints. Adding a new backtest variant (e.g. by symbol or date range) requires only a new route that queries the DB and calls `_compute_backtest()`.

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 7 Backtest Engine*  
*Generated 2026-05-30*
