# ALPHA RADAR SIGNALS V3.1 — Sprint 6 Report
## Paper Trading

**Date:** 2026-05-30  
**Sprint:** 6 — Paper Trading  
**Status:** ✅ COMPLETE

---

## Files Changed

| File | Change |
|------|--------|
| `app/database/models.py` | `PaperPosition` — added `tp2`, `tp3` columns |
| `app/database/session.py` | `_SCHEMA_UPGRADES` — `ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS tp2/tp3` |
| `app/paper/__init__.py` | New package init |
| `app/paper/trading.py` | New — paper trading engine |
| `app/main.py` | `_handle_signal` opens paper position; `_handle_tracker_event` closes on TP/SL |
| `app/dashboard/server.py` | Three new API routes; `_paper_page_html()` rewritten |

No real Binance trading API called. No real funds.

---

## `paper_positions` Table

All required columns present:

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `signal_id` | Integer FK → signals (SET NULL) | |
| `symbol` | String(32) | |
| `side` | String(8) | LONG / SHORT |
| `entry_price` | Float | `signal.entry_low` |
| `stop_loss` | Float | |
| `tp1` | Float | |
| `tp2` | Float | NEW in Sprint 6 |
| `tp3` | Float | NEW in Sprint 6 |
| `size_usdt` | Float | risk-based sizing |
| `status` | String(16) | OPEN / TP1 / TP2 / TP3 / SL / CLOSED |
| `pnl_usdt` | Float | |
| `pnl_pct` | Float | |
| `opened_at` | DateTime TZ | indexed |
| `closed_at` | DateTime TZ nullable | |

---

## Paper Trading Engine — `app/paper/trading.py`

### Constants

```python
INITIAL_BALANCE = 10_000.0   # USDT
RISK_PCT        = 0.01       # 1 % per trade
```

### Position Sizing

```
risk_usdt  = current_balance × 0.01
risk_dist  = |entry_price − stop_loss| / entry_price
size_usdt  = risk_usdt / risk_dist
```

Gives constant monetary risk per trade regardless of price level.  
Minimum fallback: `size_usdt = risk_usdt` when stop distance is zero.  
Cap: `size_usdt ≤ balance × 0.50` to prevent degenerate oversizing.

### Position Lifecycle

| Signal Event | Paper Position Update |
|---|---|
| New signal saved | `status = OPEN`, size calculated from current balance |
| TP1 fires | `status = TP1`, `pnl_pct` updated |
| TP2 fires | `status = TP2`, `pnl_pct` updated |
| TP3 fires | `status = TP3`, `pnl_usdt` realised, `closed_at` set |
| SL fires | `status = SL`, `pnl_usdt` realised (negative), `closed_at` set |

### Win Rate

- **Win** = final status is TP1, TP2, or TP3
- **Loss** = final status is SL
- Balance curve uses only TP3/SL closed positions

### Balance Calculation

Every call to `get_portfolio_stats()` replays all `FINAL_STATUSES` (TP3/SL) positions in chronological order to compute the running balance. This is accurate and requires no stored state.

---

## API Endpoints

### `GET /api/paper`

Primary endpoint. Returns stats + latest 50 open + 50 closed positions.

```json
{
  "initial_balance":  10000.0,
  "current_balance":  10000.0,
  "total_pnl_usdt":   0.0,
  "total_pnl_pct":    0.0,
  "total_trades":     0,
  "open_count":       0,
  "closed_count":     0,
  "wins":             0,
  "losses":           0,
  "win_rate":         0.0,
  "avg_pnl_pct":      0.0,
  "balance_curve":    [10000.0],
  "open":             [],
  "closed":           []
}
```

### `GET /api/paper/positions`

Positions list with optional filter.

| Query Param | Values | Default |
|---|---|---|
| `status` | `open` / `closed` / *(empty = all)* | all |
| `limit` | 1–500 | 100 |

```json
{
  "positions": [
    {
      "id": 1,
      "signal_id": 42,
      "symbol": "BTCUSDT",
      "side": "LONG",
      "entry_price": 73650.0,
      "stop_loss": 72000.0,
      "tp1": 75000.0,
      "tp2": 76500.0,
      "tp3": 78000.0,
      "size_usdt": 4500.0,
      "status": "OPEN",
      "pnl_usdt": 0.0,
      "pnl_pct": 0.0,
      "opened_at": "05-30 14:22",
      "closed_at": null
    }
  ],
  "count": 1
}
```

### `GET /api/paper/stats`

Statistics only (no positions list).

### `GET /api/public/paper` (backward-compat)

Preserved from previous sprint — still computes positions on-the-fly from the `signals` table. Kept so existing callers don't break.

---

## Page — `GET /paper`

### Header
"PAPER TRADING · Virtual Portfolio · 10 000 USDT · 1% Risk Per Trade · No Real Funds"

### Warning Banner
"Simulated only. Positions are opened automatically for every valid MTF signal. No real Binance API calls. No real money."

### 5 KPI Cards
- Balance (USDT, with starting balance sub-label)
- Total PnL (USDT + % sub-label)
- Win Rate (green ≥50%, red <50%, W/L sub-label)
- Open (count)
- Closed (count with W/L)

### Balance Curve
Bar chart of running balance across all closed trades. Green bars above starting balance, red below. Shows last 60 data points.

### Open Positions Table
Columns: Opened · Symbol · Side · Entry · SL · TP1 · TP2 · TP3 · Size · Status  
Symbol links to `/signal/{id}`.

### Closed Trades Table
Columns: Opened · Closed · Symbol · Side · Entry · SL · TP1 · Size · Status · PnL% · PnL USDT  
Symbol links to `/signal/{id}`.

Auto-refreshes every **15 seconds**.

---

## Integration Points

### New Signal → Paper Position (main.py `_handle_signal`)

After `create_signal()` succeeds:
```python
await open_paper_position(persisted)
```
Logs: `📊 paper position opened for signal #{id} SYMBOL`

### Signal Event → Paper Position Close (main.py `_handle_tracker_event`)

On TP1/TP2/TP3/SL events from the tracker:
```python
await on_signal_event(signal_id, event, pnl_pct)
```
Failures are logged as warnings (never crash the tracker).

---

## Validation

### Syntax
```
app/database/models.py    ✅ OK
app/database/session.py   ✅ OK
app/paper/trading.py      ✅ OK
app/main.py               ✅ OK
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

### API Smoke Tests
```
GET /api/paper             →   200 OK — all 13 fields present
GET /api/paper/stats       →   200 OK
GET /api/paper/positions   →   200 OK — count: 0 (no signals yet)
GET /paper                 →   200 OK (HTML page)
```

---

## Known Limitations

1. **Cold start — no backfill**: Existing signals in the database do NOT have paper positions. The engine only creates positions for new signals going forward. Run the backfill script (if implemented) to seed historical positions.

2. **TP1/TP2 partial exits**: In real trading, traders often take partial profits at TP1/TP2. The paper engine tracks these milestones but only realises PnL at the final exit (TP3 or SL). Future enhancement: partial exits at each TP level.

3. **Balance curve empty until first TP3/SL close**: The curve only shows closed trades. Until at least one trade closes at TP3 or SL, the curve shows the flat starting balance.

4. **Position size cap**: `size_usdt` is capped at 50% of current balance to prevent runaway leverage on very tight stops.

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 6 Paper Trading*  
*Generated 2026-05-30*
