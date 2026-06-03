# Stop-Loss Engine V2 — Previous-1D Support/Resistance Stop

**Date:** 2026-06-03
**Goal:** place the stop below the previous completed **1D candle support** (LONG) / above the
previous **1D resistance** (SHORT), instead of the 15m ATR/structure stop that was getting hit early.
**Constraint honored:** signal engine, scanner decision pipeline, and entry logic are **unchanged** —
only the stop-loss calculation and diagnostics were modified.

---

## What changed

### 1. Config flags (`app/config.py`, defaults; enabled in `.env`)
```python
stoploss_engine_v2_enabled: bool  = False   # STOPLOSS_ENGINE_V2_ENABLED
stoploss_method: str              = "PREV_1D_SUPPORT"  # STOPLOSS_METHOD
stoploss_1d_buffer_atr_mult: float = 0.15   # STOPLOSS_1D_BUFFER_ATR_MULT  (buffer = mult * ATR(1D))
min_sl_distance_percent: float    = 2.0     # MIN_SL_DISTANCE_PERCENT
max_sl_distance_percent: float    = 10.0    # MAX_SL_DISTANCE_PERCENT
stoploss_too_close_action: str    = "widen" # widen | reject
```
`.env` sets `STOPLOSS_ENGINE_V2_ENABLED=true` so the live bot uses V2. Disabling the flag restores
the legacy 15m ATR/structure stop with zero code changes.

### 2. SL computation (`app/risk/levels.py`)
- **`compute_prev_1d_stop(side, entry_price, prev_1d_low, prev_1d_high, atr_1d, ...)`** — pure,
  testable, never raises. Returns the SL + full diagnostics dict.
  - LONG: `stop_loss = prev_1d_low − buffer`; SHORT: `stop_loss = prev_1d_high + buffer`.
  - `buffer = stoploss_1d_buffer_atr_mult × ATR(1D)`.
- **`_extract_prev_1d(df_1d)`** — pulls the **previous completed** daily candle. Binance returns the
  in-progress candle as the last row, so this uses `iloc[-2]` and computes `ATR(1D)`.
- **`build_levels(..., df_1d=None)`** — gains an optional `df_1d` arg. When V2 is enabled and a 1D
  frame is available it uses the 1D stop; otherwise it falls back to the legacy stop (also when 1D
  data is missing, so a feed gap never silently kills all signals). TP1/TP2/TP3 and RR are derived
  from the resulting `risk` exactly as before (RR gate `min_rr` still applies).

### 3. Safety rules (per spec)
1. **Correct side** — LONG `stop_loss < entry`, SHORT `stop_loss > entry`; else **reject**
   (`support_not_below_entry` / `resistance_not_above_entry`).
2. **Too far** — `sl_distance% > MAX_SL_DISTANCE_PERCENT` → **reject** (`sl_too_far`). *(reject chosen
   over fallback, as requested, for safety.)*
3. **Too close** — `sl_distance% < MIN_SL_DISTANCE_PERCENT` → **widen** to the floor (default;
   `sl_widened=true`) or **reject** (`sl_too_close`) when `STOPLOSS_TOO_CLOSE_ACTION=reject`.

A V2 rejection makes `build_levels` return `None`, which the scanner already treats as a normal
signal rejection (logged with the reason) — no new scanner control flow.

### 4. Diagnostics (`app/scanner/scanner.py`)
The SL diagnostics are merged into the existing signal `diagnostics` JSON (an allowed change):
```json
{
  "stoploss_method": "PREV_1D_SUPPORT",
  "prev_1d_low": 0.5123, "prev_1d_high": 0.5460, "prev_1d_time": "2026-06-02T00:00:00+00:00",
  "base_sl": 0.5123, "sl_buffer": 0.0021,
  "stop_loss": 0.5102, "sl_distance_percent": 4.6,
  "sl_valid": true, "sl_reject_reason": null
}
```
The legacy path also records `stoploss_method` (`atr`/`structure`) and `sl_distance_percent` so every
signal is comparable.

### 5. Analytics (`app/analytics/winrate.py`)
`compute_winrate_analysis` now also returns:
- `sl_method_buckets` — winrate by `PREV_1D_SUPPORT` / `atr` / `structure` / `liquidity` (1D support
  stop vs ATR stop vs structure stop).
- `sl_distance_buckets` — winrate by `0-2% / 2-4% / 4-6% / 6-10% / 10%+` (derived from the stored
  diagnostic, or from `entry_mid` vs `stop_loss` for older rows).
- `best_sl_method`.

---

## Files changed
| File | Change |
|------|--------|
| `app/config.py` | 6 new V2 stop-loss config flags. |
| `app/risk/levels.py` | `compute_prev_1d_stop`, `_extract_prev_1d`, `TradeLevels.sl_diag`, V2 branch + safety guards in `build_levels` (optional `df_1d`). |
| `app/scanner/scanner.py` | Pass `df_1d` to `build_levels`; merge `sl_diag` into diagnostics. (No decision-logic change.) |
| `app/analytics/winrate.py` | SL-method + SL-distance winrate buckets. |
| `.env` | Enable V2 (`STOPLOSS_ENGINE_V2_ENABLED=true`) + flags. |
| `tests/test_stoploss_engine_v2.py` | New unit tests. |

---

## Tests (`tests/test_stoploss_engine_v2.py`)
1. LONG entry=100, prev_1d_low=95, buffer=1 → **SL=94** ✓
2. SHORT entry=100, prev_1d_high=105, buffer=1 → **SL=106** ✓
3. LONG invalid (support above entry) → **reject** ✓
4. SHORT invalid (resistance below entry) → **reject** ✓
5. Too far (dist 21% > 10%) → **reject** (`sl_too_far`) ✓
6. Too close (dist < 2%) → **widen to 2%** (and reject-mode variant) ✓
   plus: buffer scales with ATR; `_extract_prev_1d` uses `iloc[-2]`; insufficient-data guard.

---

## Validation
```
python -m compileall app tests            → OK (exit 0)
pytest -q tests/test_stoploss_engine_v2.py → 11 passed
pytest -q  (full suite)                    → 292 passed, 2 warnings
docker compose build bot                   → Image built
docker compose up -d bot                   → Recreated & started
docker logs signals-bot --since=3m | grep -Ei "error|traceback|exception|syntaxerror|importerror"
                                           → no matches (clean boot)
```

---

## Notes / follow-ups
- V2 widens stops materially vs the old 15m stop. The prior audit
  (`STOPLOSS_ENGINE_AUDIT_REPORT.md`) showed 0–2% stops winning ~14% vs ~50% at 6–10%, so the
  direction matches the data — monitor live winrate via the new `sl_distance_buckets`.
- Backtester (`app/backtesting/historical.py`) still calls `build_levels` without `df_1d`, so it
  stays on the legacy stop until 1D frames are wired there (out of scope; not a signal-engine change).
- The previously-noted instrumentation gap (regime/rr_method not persisted to dedicated columns) is
  unrelated to this change; SL diagnostics here live in the `diagnostics` JSON, which is persisted.
