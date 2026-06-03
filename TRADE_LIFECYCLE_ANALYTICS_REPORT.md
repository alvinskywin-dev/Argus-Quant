# Sprint 22C — Trade Lifecycle Analytics

## Goal
Measure trade *quality* after entry, independent of win/loss: MFE, MAE, time to
TP1/SL/TP2, and derived entry / SL / TP quality scores — then aggregate to find
optimal SL/TP distances and regime-specific performance.

## What shipped
- `app/analytics/trade_lifecycle.py` — pure computation.
- 18 unit tests in `tests/test_trade_lifecycle.py`.
- `TRADE_LIFECYCLE_ANALYTICS_ENABLED` flag.

## Tracked per trade (`TradeLifecycle`)
1. MFE % (max favourable excursion) 2. MAE % (max adverse excursion)
3. time_to_tp1 / time_to_sl / time_to_tp2 4. entry_quality_score (0-100)
5. sl_quality_score 6. tp_quality_score 7. recovery_after_drawdown
8. volatility_during_trade 9. outcome.

## Quality scoring
- **Entry quality** — high when MAE is small relative to risk (good timing).
- **SL quality** — penalised when a stop is hit *after* a large MFE (too tight);
  rewarded when winners had small MAE relative to risk.
- **TP quality** — fraction of the realised MFE that the target captured.

## Aggregation (`aggregate_lifecycles`)
- average MFE before SL, average MAE before TP, avg MFE/MAE
- **optimal SL distance** ≈ worst winner MAE × 1.1 (so winners would not have
  been stopped)
- **optimal TP distance** ≈ median MFE
- avg entry/SL/TP quality, avg time-to-TP1 / time-to-SL
- per-regime breakdown (trades, avg MFE/MAE, avg TP quality).

## Database
The existing `Signal` model already persists `max_favorable_pct` and
`max_adverse_pct`; the engine both **computes** these from an intratrade price
path and **consumes** them when no path is available, plus carries the extended
metrics (time-to-X, quality scores) in the existing `diagnostics` JSON column —
so **no migration is required** and recovery/reconciliation are untouched.

## Safety / compatibility
Pure functions, no I/O, no DB writes from the engine. Empty/partial trades
degrade to a well-formed zeroed result (no fake data).

## Validation
`compileall` ✓ · `ruff` ✓ · `black` ✓ · `pytest tests/test_trade_lifecycle.py`
→ 18 passed.
