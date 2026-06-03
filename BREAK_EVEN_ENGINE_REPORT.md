# Sprint 22D — Break-Even + Partial TP Engine

## Goal
Protect profit after TP1: close a slice, move the stop to entry, and arm a
trailing stop on the remainder.

## What shipped
- `app/live_trading/break_even.py` — a **planner** (emits intents, executes
  nothing).
- 21 unit tests in `tests/test_break_even_engine.py`.
- Config block + `.env.example`.

## Behaviour on TP1 (`plan_tp1_actions`)
1. `PARTIAL_CLOSE` reduce-only for `PARTIAL_TP_PERCENT` (default 40%) of size.
2. `MOVE_SL` to entry (break-even) when `MOVE_SL_TO_ENTRY_ON_TP1=true`.
3. `ARM_TRAILING` at `TRAILING_STOP_DISTANCE_PERCENT` (default 1.5%) on the rest.

`update_trailing_stop()` then ratchets the stop in the favourable direction only.

## Design: planner, not executor
The engine returns a list of `BreakEvenIntent` (PARTIAL_CLOSE / MOVE_SL /
ARM_TRAILING / UPDATE_TRAILING). The existing live/paper execution layer turns
an intent into an order — so reconciliation, recovery, the emergency-close path
and the live safety gate stay fully in charge. There is intentionally **no code
path from this module to an exchange adapter.**

## Hard invariants (enforced regardless of config)
- **SL is never widened** — `_is_tighter()` only permits a higher stop for LONG
  / lower for SHORT; tested on both sides.
- All closes are **reduce-only** and never exceed open size.
- **Idempotent**: actions recorded in `PositionProtectionState` are not
  re-emitted (no duplicate TP/SL orders).
- Flag off → no intents produced.

## Diagnostics
`partial_tp_executed`, `break_even_activated`, `trailing_stop_active`,
`realized_partial_pnl`.

## Validation
`compileall` ✓ · `ruff` ✓ · `black` ✓ · `pytest tests/test_break_even_engine.py`
→ 21 passed.
