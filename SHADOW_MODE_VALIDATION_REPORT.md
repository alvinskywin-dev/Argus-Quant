# Sprint 22G — Shadow Mode Live Validation

## Goal
Answer "what would have happened if this signal executed live?" — entry, TP/SL,
latency, slippage, missed-fill probability — **without ever placing a real
order**, then compare paper vs. hypothetical-live vs. actual market movement.

## What shipped
- `app/live/shadow_mode.py` — pure arithmetic over price data.
- 22 unit tests (incl. **safety tests**) in `tests/test_shadow_mode.py`.
- Config: `SHADOW_MODE_ENABLED`, `SHADOW_MODE_SLIPPAGE_BPS`,
  `SHADOW_MODE_LATENCY_MS`.

## Engine surface
- `simulate_entry_fill(side, price, next_candle)` → `ShadowFill`
  (adverse slippage, latency, miss probability/filled).
- `simulate_signal(signal, price_path, paper_pnl_percent, next_candle)` →
  `ShadowResult` (hypothetical entry/TP/SL, outcome, hypothetical PnL with
  adverse exit slippage, TP/SL sync realism, actual market move).
- `build_report(results)` → `ShadowReport`: shadow winrate, paper winrate,
  avg slippage / latency / missed-fill, slippage impact (paper − shadow PnL),
  TP/SL sync realism.

## Tracked
1. hypothetical entry 2. hypothetical TP 3. hypothetical SL 4. hypothetical PnL
5. execution latency 6. slippage estimate 7. missed-fill probability
8. TP/SL sync simulation — plus a paper-vs-shadow-vs-actual comparison.

## ╔ HARD SAFETY GUARANTEE ╗
This module imports **no** exchange client, places **no** orders, sends **no**
execution request and modifies **no** exchange state. There is intentionally no
code path from here to an adapter.

Enforced by tests:
- `test_module_declares_no_real_orders` — asserts `__places_real_orders__ is False`.
- `test_module_does_not_import_exchange_client` — greps the module source and
  fails if it references `exchange_adapters`, `binance_client`, `place_order`,
  `create_order` or `get_client`.

## Reporting
A "Shadow vs Paper" dashboard view is fed by `ShadowReport`: shadow winrate,
slippage impact, latency impact and TP/SL execution realism.

## Validation
`compileall` ✓ · `ruff` ✓ · `black` ✓ · `pytest tests/test_shadow_mode.py`
→ 22 passed.
