# StopLoss V3 — Balanced Mode Report

**Project:** ARGUS QUANT — Futures Signal Bot
**Change:** Add a balanced ATR/structure stop engine to relieve signal starvation
without deleting StopLoss V2, removing the previous-1D stop, or touching the
entry engine, scanner strategy, or risk management.

---

## 1. Why the Previous-1D stop caused signal starvation

StopLoss V2 (`STOPLOSS_METHOD=PREV_1D_SUPPORT`) anchors the stop to the **previous
completed daily candle**:

- LONG: `stop_loss = prev_1d_low − buffer`
- SHORT: `stop_loss = prev_1d_high + buffer`

Entries are taken on the **15m** timeframe. The previous daily low/high is
frequently several percent away from a 15m entry, so:

1. **SL distance is large** → often exceeds `MAX_SL_DISTANCE_PERCENT`, tripping
   the `sl_too_far` reject inside `compute_prev_1d_stop`.
2. **When it is not rejected, the risk leg (`entry − sl`) is large** → RR to TP2
   (`(tp2 − entry) / risk`) collapses below `MIN_RR`, so `build_levels` returns
   `None` and the setup dies at the RR stage.

The observed logs are the signature of this: confidence passes a few candidates,
**RR pass = 0**, **emitted = 0**, and the dominant reject reason is
`SL V2 reject: sl_too_far`.

## 2. What Balanced Mode changes

A new engine, **StopLoss V3 Balanced** (`app/risk/stoploss_modes.py`), sizes the
stop from **15m volatility + 15m structure** instead of the daily candle, and
clamps it to a sane distance band. Two candidates are formed:

| Candidate | LONG | SHORT |
|-----------|------|-------|
| ATR stop | `entry − ATR₁₅ₘ · 2.2` | `entry + ATR₁₅ₘ · 2.2` |
| Structure stop | `recent_swing_low − ATR₁₅ₘ · 0.25` | `recent_swing_high + ATR₁₅ₘ · 0.25` |

Selection (the *safer but not excessively far* stop):

1. Pick the **more conservative** candidate (lower for LONG, higher for SHORT)
   that is still on the correct side of entry.
2. If its distance **exceeds max** → **fall back to the ATR stop**.
3. If the ATR stop is **still beyond max** → **reject** (`sl_too_far`).
4. If the chosen stop is **closer than min** → **widen to the min-distance floor**.

Because the stop now scales to 15m ATR (typically well under the daily range),
SL distance lands inside `[1.8%, 8%]` for most setups, the risk leg shrinks, and
RR clears `MIN_RR` far more often — relieving starvation **without forcing any
emission**. Invalid inputs still produce `sl_valid=False` and the setup is
dropped.

## 3. Comparison of the three modes

| | `LEGACY_ATR` | `PREV_1D_SUPPORT` (V2) | `BALANCED` (V3, default) |
|---|---|---|---|
| Anchor | 15m swing/ATR (`min(swing−0.2·ATR, price−1.8·ATR)`) | Previous **1D** candle low/high + buffer | 15m ATR **and** 15m swing, best-of |
| Typical SL distance | Moderate | **Wide** (daily range) | Tight–moderate (`1.8–8%`) |
| Signal starvation risk | Low | **High** | Low |
| Min/max distance guard | (legacy, none explicit) | `MIN/MAX_SL_DISTANCE_PERCENT` | `BALANCED_STOP_MIN/MAX_DISTANCE_PERCENT` |
| Structure→ATR fallback | n/a | n/a | **Yes** |
| Use case | Simplest baseline | Conservative / institutional | **Default — balanced** |

All three remain selectable; nothing was deleted.

## 4. Safety rules (unchanged risk posture)

- LONG SL must be **below** entry; SHORT SL must be **above** entry. Candidates on
  the wrong side are discarded; if none remain → reject
  (`no_candidate_below/above_entry`).
- SL distance `<` min → **widen** to the min-distance floor (never tighter).
- SL distance `>` max → **fall back to ATR**; if still `>` max → **reject**
  (`sl_too_far`).
- Previous-1D support is **never** consulted in Balanced mode unless
  `BALANCED_STOP_ALLOW_1D_FALLBACK=true`.
- Non-finite / non-positive inputs → reject; the function never raises and never
  fabricates a stop.
- **No emission is forced**: a rejected stop returns `None` from `build_levels`
  exactly as before.

## 5. Regime-adaptive interaction

When `REGIME_ADAPTIVE_GATE_ENABLED=true`, the Balanced **max distance** adapts by
regime (`balanced_max_distance_percent`):

| Regime | Max SL distance |
|--------|-----------------|
| LOW_VOLATILITY | `LOW_VOL_BALANCED_MAX_DISTANCE_PERCENT` (12%) |
| HIGH_VOLATILITY | `HIGH_VOL_BALANCED_MAX_DISTANCE_PERCENT` (6%) |
| SIDEWAYS | `SIDEWAYS_BALANCED_MAX_DISTANCE_PERCENT` (6%) |
| other / gate off | `BALANCED_STOP_MAX_DISTANCE_PERCENT` (8%) |

`MIN_RR` and confidence remain owned by the existing Regime Adaptive Gate and its
hard clamps (`HARD_MIN_RR=1.0`); **the min RR is never lowered below that clamp.**

## 6. Diagnostics (Phase 4)

`build_levels(...).sl_diag` carries, for Balanced setups:

```json
{
  "stoploss_engine_mode": "BALANCED",
  "stoploss_method": "ATR_STRUCTURE_BALANCED",
  "balanced_atr_stop": 0.521,
  "balanced_structure_stop": 0.514,
  "selected_stop_source": "ATR",
  "sl_distance_percent": 3.2,
  "sl_min_distance_percent": 1.8,
  "sl_max_distance_percent": 8.0,
  "sl_reject_reason": null
}
```

The public risk endpoint `/api/public/regime-adaptive-thresholds` now also returns
`stoploss_engine_mode`, `balanced_max_distance`, and `balanced_atr_mult`.

## 7. Tests run

New: `tests/test_stoploss_v3_balanced.py` (12 cases — all the Phase-7 items):
LONG/SHORT ATR valid, LONG/SHORT structure valid, structure→ATR fallback, ATR
too-close widen, too-far reject, PREV_1D_SUPPORT still works, LEGACY_ATR still
works (+ back-compat resolution), regime LOW_VOL widen / HIGH_VOL+SIDEWAYS
tighten, diagnostics contain `selected_stop_source`.

```
python -m compileall app tests          # OK
pytest -q tests/test_stoploss_v3_balanced.py   # 12 passed
pytest -q                                # 542 passed
ruff check .                             # All checks passed
black --check .                          # clean
docker compose build bot                 # Image built
```

## 8. Recommended `.env` after deploy

```env
STOPLOSS_ENGINE_MODE=BALANCED
REGIME_ADAPTIVE_GATE_ENABLED=true
LOW_VOL_MIN_RR=1.0
LOW_VOL_MAX_SL_DISTANCE_PERCENT=15.0
LOW_VOL_MIN_CONFIDENCE_DELTA=-3

# Balanced stop
BALANCED_STOP_ATR_MULT=2.2
BALANCED_STOP_MIN_DISTANCE_PERCENT=1.8
BALANCED_STOP_MAX_DISTANCE_PERCENT=8.0
LOW_VOL_BALANCED_MAX_DISTANCE_PERCENT=12.0
```

(Defaults are baked into `app/config.py`, so an empty `.env` already runs
Balanced mode.)

## 9. Rollback plan

Balanced mode is config-only — no code rollback required:

1. **Back to V2 (1D) stop:** set `STOPLOSS_ENGINE_MODE=PREV_1D_SUPPORT` (or
   `STOPLOSS_ENGINE_V2_ENABLED=true` with `STOPLOSS_ENGINE_MODE` empty). Behaves
   exactly as before this change.
2. **Back to the legacy 15m stop:** set `STOPLOSS_ENGINE_MODE=LEGACY_ATR`.
3. Restart the bot to pick up the new `.env` (not done automatically).
4. Full code revert if ever needed: revert the single commit
   *"Add StopLoss V3 balanced ATR-structure mode"* — V2 and legacy code paths are
   untouched and remain functional.
