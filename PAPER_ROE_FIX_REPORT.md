# Paper Trading — ROE / PnL Realtime Calculation Fix

**Branch:** `feature/timezone-system-v1`
**Scope:** per-user paper-trading engine (`/api/paper/account/*`) + price cache + SaaS portal paper page.
**No trading-logic / scoring / live-gate / adapter changes.**

---

## 1. Root cause

Open paper positions always rendered **ROE = 0.00%**, **PnL = $0.00**, **Mark = Entry**, even after the
market moved. Two compounding defects:

### a) The price cache only tracked 3 hardcoded symbols
`app/market_data/ws_engine.py` polled a fixed whitelist:

```python
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
```

`latest_prices` was therefore populated **only** for those three. A paper position on any other
symbol (the vast majority of what the scanner trades) had **no entry in the cache at all**.

### b) The mark-price helper silently fell back to `entry_price`
Both the service helper and the router used `entry_price` as the fallback when the cache missed:

```python
# service.py
def mark_price(symbol, fallback=0.0):
    return float(latest_prices.get(symbol, fallback) or fallback)

# router._position_out
pmath_mark = service.mark_price(p.symbol, p.entry_price)   # ← fallback = entry
# account_summary
px = mark_price(p.symbol, p.entry_price)                   # ← fallback = entry
```

So on every cache miss `mark == entry`, which makes the move `mark/entry − 1 == 0`:

```
unrealized_pnl = notional * 0 = 0   →   PnL = $0.00
roe            = 0 / margin   = 0   →   ROE = 0.00%
mark           = entry              →   "Mark = Entry"
```

The position **looked** live (it had a mark) but the number was a phantom — exactly the reported symptom.

---

## 2. The fix

### Mark price is now symbol-agnostic, on-demand, and never entry

**`app/market_data/ws_engine.py`**
- Replaced the fixed `SYMBOLS` whitelist with a dynamic `_tracked` set (seeded with
  `DEFAULT_SYMBOLS`) plus `register_symbols()`.
- Added per-symbol freshness tracking: `price_updated_at` + `price_age()`.
- Added `fetch_price()` (one symbol, live) and `ensure_prices()` (register + fetch any
  **missing or stale** symbols, in parallel). Stale = older than `STALE_AFTER_SEC = 5s`.
- The polling loop now iterates the dynamic set, so any symbol with an open position is kept fresh
  every ~2s; `ensure_prices` only does network work on the first sighting of a symbol.

**`app/paper_engine/service.py`**
- `mark_price(symbol)` **no longer accepts/returns an `entry_price` fallback** — it returns the live
  cached price or `0.0`. `0.0` means "no mark yet", never "marked at entry".
- Added `ensure_marks(symbols)` (wraps `ws_engine.ensure_prices`) and `mark_price_info(symbol)`
  → `(price, source, age)` for diagnostics.
- `account_summary`, `check_liquidations`, `close_position` now call `ensure_marks(...)` first and
  **skip** PnL contribution for any position with no live mark (`px > 0` guard) instead of marking at
  entry.
- `open_position` registers its symbol for ongoing polling immediately.

**`app/paper_engine/router.py`**
- `_position_out` uses `service.mark_price(symbol)` (no entry fallback); when there is no live mark it
  emits `mark_price / unrealized_pnl / roe_pct = None` (the UI shows "—") rather than a phantom 0.
- `positions`, `open`, `copy` call `ensure_marks(...)` before serialising so the response carries a
  fresh mark.

### Formulas (unchanged math, now actually fed a real mark)
`app/paper_engine/math.py` already implemented the canonical isolated-margin maths; with a real mark
they now resolve to the spec:

```
LONG :  roe = (mark - entry)/entry * leverage * 100 ;  pnl = (mark - entry) * qty
SHORT:  roe = (entry - mark)/entry * leverage * 100 ;  pnl = (entry - mark) * qty
```

### Diagnostics endpoint
**`GET /api/debug/paper-positions`** (auth required) returns, per open position:
`symbol, side, entry_price, mark_price, qty, leverage, notional_usdt, margin_usdt, roe, pnl,
price_source` (`live`/`none`), `last_price_update` (age in seconds).

### Frontend — realtime, no page reload
**`app/dashboard/static/saas/saas.js`**
- Added a dedicated 1-second poller (`startFastTimer`/`stopFastTimer`, cleared by the router teardown
  via `stopPageTimer`).
- The Paper → Open Positions tab now refreshes **Mark / ROE / PnL every second**, patching the
  existing cards in place (`patchCards`) — no flicker, no scroll jump, no full reload. It only
  re-renders when the set of open positions changes; switching to Trade History stops the poller.

---

## 3. Files changed

| File | Change |
|------|--------|
| `app/market_data/ws_engine.py` | Dynamic symbol set, on-demand `fetch_price`/`ensure_prices`, per-symbol `price_updated_at`/`price_age`, `register_symbols`. |
| `app/paper_engine/service.py` | `mark_price` never defaults to entry; add `ensure_marks`, `mark_price_info`; ensure-then-compute in summary/liquidation/close; register symbol on open. |
| `app/paper_engine/router.py` | `_position_out` uses live mark (None when absent); ensure marks in `positions`/`open`/`copy`; new `GET /api/debug/paper-positions`. |
| `app/paper_engine/__init__.py` | Mount the diagnostics `debug_router`. |
| `app/dashboard/static/saas/saas.js` | 1s realtime in-place refresh of open-position Mark/ROE/PnL. |
| `tests/test_paper_roe.py` | New regression tests (price up/down, long/short, 10x, no-mark→None). |

---

## 4. Before / after

The paper page is gated behind auth + `PAPER_TRADING_ENABLED` and needs Postgres/Redis + a live
Binance feed, which are not reachable from the CI sandbox, so live screenshots could not be captured
here. The behavioural change is captured deterministically by the regression tests below, which
reproduce the exact failure and assert the fix.

**Before** (cache miss → entry fallback):

| Field | Value |
|-------|-------|
| Mark  | = Entry |
| ROE   | 0.00% |
| PnL   | $0.00 |

**After** (LONG, entry 100, 10x, live mark 110):

| Field | Value |
|-------|-------|
| Mark  | 110.0000 |
| ROE   | +100.00% |
| PnL   | +$100.00 |

`GET /api/debug/paper-positions` (after):
```json
{
  "open_positions": 1,
  "positions": [{
    "symbol": "BTCUSDT", "side": "LONG",
    "entry_price": 100.0, "mark_price": 110.0,
    "qty": 10.0, "leverage": 10,
    "roe": 100.0, "pnl": 100.0,
    "price_source": "live", "last_price_update": 0.4
  }]
}
```

---

## 5. Validation

```
$ python3 -m pytest tests/test_paper_roe.py tests/test_paper_engine.py -q
23 passed

$ python3 -m pytest -q          # full suite
281 passed, 2 warnings
```

New tests (`tests/test_paper_roe.py`):
- ROE/PnL match spec for LONG & SHORT at 10x, price up and down.
- `+10%` move at 10x ⇒ exactly `+100%` ROE (long and short).
- `mark_price` returns `0.0` — **never entry** — when the symbol is absent; reads the live cache when present.
- `_position_out` reflects the live mark on price-up and price-down, and emits `None` (not a phantom 0)
  when no live mark exists.
- `node --check saas.js` ⇒ syntax OK.
