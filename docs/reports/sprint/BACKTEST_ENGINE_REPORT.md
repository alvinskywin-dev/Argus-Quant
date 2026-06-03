# Real Historical Backtest Engine
**Date:** 2026-05-30  
**Sprint:** V3.3 Priority #2  
**Status:** Complete

---

## 1. Architecture

### Data flow

```
User selects: Symbol · Start Date · End Date · Strategy Version
           ↓
GET /api/backtest/run
           ↓
HistoricalBacktestEngine.run()
  ├── _fetch_all()  — async: 4× klines_range() calls to Binance REST
  │     15m   1h   4h   1d
  │     each prefixed with 250 warm-up bars
  │
  └── _replay()    — sync (thread-pool executor)
        │
        ├── iterate each 15M candle in test window
        │     ├── cache-invalidate 1D/4H/1H snapshots on new close
        │     ├── fast 1D trend pre-check (skip 4H/1H/15M if no trend)
        │     ├── build_snapshot() × 4 TFs
        │     ├── evaluate_pipeline()  ← identical to live scanner
        │     ├── build_levels()       ← identical to live scanner
        │     └── _simulate_exit()     ← scan forward for TP/SL hit
        │
        └── _compute_metrics()  → BtResult
```

### Entry point

`app/backtesting/historical.py` → `HistoricalBacktestEngine`

### API endpoint

```
GET /api/backtest/run
    ?symbol=BTCUSDT
    &start=2025-01-01
    &end=2025-03-31
    &strategy=V3.2
```

Returns `BtResult.to_dict()` as JSON.

---

## 2. Files Changed

| File | Change |
|------|--------|
| `app/market_data/binance_client.py` | Added `start_time`/`end_time` params to `klines()`, new `klines_range()` batch fetcher |
| `app/market_data/klines.py` | New `fetch_klines_historical(symbol, interval, start_ms, end_ms)` |
| `app/backtesting/historical.py` | **New** — full candle-replay engine (350 lines) |
| `app/backtesting/__init__.py` | Export `HistoricalBacktestEngine`, `BtResult` |
| `app/dashboard/server.py` | New `GET /api/backtest/run` endpoint; rewritten `/backtest` page |

---

## 3. Backtest Flow Detail

### Data fetching

- 4 parallel `klines_range()` calls (asyncio.gather)
- Each TF prefetches `_WARMUP_BARS = 250` bars before `start_date`
  - 1D: 250 days of history → stable EMA200 from day 1 of test window
  - 4H: 250 × 4h = ~41 days of history
  - 1H: 250 × 1h = ~10 days of history
  - 15M: 250 × 15m = ~2.6 days of history
- Batch size: 1500 candles per REST request; 50 ms pause between batches

### Candle replay

For each 15M candle in `[start_date, end_date]`:

1. **Snapshot caching** — higher-TF snapshots rebuilt only when that TF closes a new candle.  
   Rebuilds per 90-day window: 1D×90, 4H×540, 1H×2160, 15M×8640 (max). In practice ~60-70% fewer 15M pipeline runs after 1D trend pre-check.

2. **Fast trend pre-check** — if `snap_d1.ema_50` ≤ `snap_d1.ema_200` (no clear 1D trend), the candle is skipped without building 4H/1H/15M snapshots.

3. **Full pipeline** — `evaluate_pipeline(snaps)` from `app/ai_scoring/mtf.py`. Exact same code path as the live scanner.

4. **Level calculation** — `build_levels(snap_15m, side)` from `app/risk/levels.py`. Same as live.

5. **Exit simulation** — scan forward up to `_MAX_HOLD = 200` candles (~50 h):
   - SL checked first on each candle (pessimistic/conservative)
   - Then TP3 → TP2 → TP1 in descending priority
   - If nothing hits within MAX_HOLD → EXPIRED, closed at the candle's close

6. **Trade blocking** — no new signal while a trade is open (`active_trade_end_i`). One trade at a time per symbol. This is slightly more conservative than the live system's 30-min cooldown.

### PnL calculation

```
LONG:  pnl_pct = (exit_price − entry_price) / entry_price × 100
SHORT: pnl_pct = (entry_price − exit_price) / entry_price × 100
```

Entry price = close of the signal candle (no slippage modelling).

---

## 4. Metrics Computed

| Metric | Description |
|--------|-------------|
| Win Rate | % of closed trades hitting TP1/TP2/TP3 |
| Profit Factor | gross profit / gross loss |
| Average RR | mean of `levels.risk_reward` across all closed trades |
| Max Drawdown | peak-to-trough of cumulative PnL% curve |
| Sharpe Ratio | mean PnL% / std dev of PnL% (population) |
| Monthly Returns | per-month: trades, wins, losses, win_rate, total_pnl, profit_factor |
| Equity Curve | cumulative PnL% at each trade close (max 121 points) |
| RR Distribution | histogram of realized RR buckets (0.5 width) |

---

## 5. Dashboard

`GET /backtest` now shows:

1. **Control panel** — Symbol, Start Date, End Date, Strategy Version, Run button  
2. **Loading spinner** — shown while simulation runs (20–60 s)  
3. **Results section** (revealed after run):
   - 8 KPI cards (same metrics as above)
   - Equity curve (bar chart)
   - Summary stats list
   - RR distribution bars
   - Monthly returns table
   - Trade log (entry time, side, entry/exit price, status, PnL%, confidence, RR, hold candles)
4. **Legacy section** — existing all-time signal DB metrics remain visible at the bottom for reference

---

## 6. Performance

| Scenario | 15M candles | Estimated time |
|----------|-------------|---------------|
| 30-day BTC  |  ~2 880 | ~8–15 s |
| 90-day BTC  |  ~8 640 | ~20–40 s |
| 365-day BTC | ~35 040 | ~80–120 s |

The replay runs in a `ThreadPoolExecutor` so the FastAPI event loop stays unblocked throughout. Other dashboard routes remain responsive during a backtest run.

---

## 7. Known Limitations

| Limitation | Impact | Future fix |
|------------|--------|-----------|
| No slippage modelling | Entry price = signal-candle close; real fills will differ by 0.1–0.3% | Add configurable slippage % |
| No funding rate | Holds longer than 8 h accrue funding; not deducted | Fetch funding history from Binance |
| One trade at a time | Live scanner can overlap symbols; backtest is single-symbol | Run per-symbol and aggregate |
| SL-first on ambiguous candle | Slightly pessimistic for candles that touch both SL and TP | Optional optimistic flag |
| No market quality filters | `passes_market_filters()` (ADX, RSI, market bias) is bypassed | Wire in filter step after pipeline |
| Max 366-day limit | Larger ranges rejected | Implement pagination/background jobs |
| No result caching | Every run re-fetches and re-computes | Add `BacktestRun` DB table |

---

## 8. Example API Call

```bash
curl "http://localhost:8010/api/backtest/run?symbol=BTCUSDT&start=2025-01-01&end=2025-03-31"
```

Sample response structure:
```json
{
  "symbol": "BTCUSDT",
  "start_date": "2025-01-01",
  "end_date": "2025-03-31",
  "strategy_version": "V3.2",
  "candles_scanned": 8641,
  "signals_generated": 12,
  "total_trades": 12,
  "wins": 8,
  "losses": 3,
  "expired": 1,
  "win_rate": 66.7,
  "avg_pnl": 1.84,
  "avg_rr": 2.21,
  "profit_factor": 3.12,
  "sharpe_ratio": 1.43,
  "max_drawdown_pct": 4.2,
  "equity_curve": [0.0, 2.3, 4.1, ...],
  "monthly": [...],
  "rr_distribution": [...],
  "trades": [...]
}
```
