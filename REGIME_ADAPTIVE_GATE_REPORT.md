# Regime Adaptive Gate V1 — Report

**Date:** 2026-06-03
**Status:** ✅ Implemented, tested, image builds.
**Flag:** `REGIME_ADAPTIVE_GATE_ENABLED=false` by default — behaviour identical
to before until enabled.
**Tests:** 393 passing (8 new in `tests/test_regime_adaptive_gate.py`).

---

## 1. Root cause

In LOW_VOLATILITY / range markets the StopLoss Engine V2 places the stop at the
**previous-1D support/resistance**, which can sit far from a 15m entry. The
resulting SL distance exceeds `MAX_SL_DISTANCE_PERCENT`, so `compute_prev_1d_stop`
returns `sl_too_far` and `build_levels` yields no candidate meeting the static
`MIN_RR`. Every setup fails RR → `RR pass: 0`, `Emitted: 0`. The thresholds were
fixed regardless of regime.

## 2. Fix (what was added — and what was NOT touched)

A **Regime Adaptive Gate** that swaps the *static* RR / SL-distance / confidence
thresholds for *regime-aware effective* ones, within hard clamps. It only
changes the numbers the existing checks compare against — it does **not** modify
the signal engine, entry logic, scanner structure, SL V2 maths, or safety, and
it **never forces emission**. With the flag off it returns the base thresholds
unchanged.

## 3. Files changed

| File | Change |
|------|--------|
| `app/config.py` | Flag + per-regime thresholds |
| `app/risk/regime_adaptive_gate.py` | **New** — `get_effective_thresholds()` + `RegimeAdaptiveThresholds` (with hard clamps + diagnostics) |
| `app/risk/filters.py` | `passes_market_filters(..., min_confidence=None)` override (logic unchanged) |
| `app/risk/levels.py` | `build_levels(..., min_rr=None, max_sl_distance_percent=None)` override (logic unchanged) |
| `app/scanner/scanner.py` | Compute effective thresholds before the confidence / SL / RR checks; use them; per-cycle log; diagnostics |
| `app/dashboard/routes/analytics_router.py` | `GET /api/public/regime-adaptive-thresholds` |
| `.env.example` | Documented config |
| `tests/test_regime_adaptive_gate.py` | **New** — 8 tests |

## 4. Config added

`REGIME_ADAPTIVE_GATE_ENABLED` (default false) plus per-regime
`*_MIN_RR`, `*_MAX_SL_DISTANCE_PERCENT`, `*_MIN_CONFIDENCE_DELTA` for
NORMAL / LOW_VOL / HIGH_VOL / BULL / BEAR / SIDEWAYS.

## 5. Threshold table by regime (defaults)

| Regime | min_rr | max_sl_distance % | confidence Δ | Intent |
|--------|:-----:|:-----------------:|:-----------:|--------|
| NORMAL | 1.5 | 10 | 0 | baseline / unknown fallback |
| LOW_VOLATILITY | 1.0 | 15 | −3 | relax (root-cause fix) |
| HIGH_VOLATILITY | 1.8 | 8 | +3 | tighten |
| BULL | 1.3 | 12 | −2 | moderate |
| BEAR | 1.3 | 12 | −2 | moderate |
| SIDEWAYS | 1.6 | 8 | +3 | tighten |

## 6. Safety clamps (always applied)

- `effective_min_rr = max(1.0, …)` — never below **1.0**
- `effective_max_sl_distance_percent = min(20.0, …)` — never above **20.0**
- `effective_min_confidence = max(70.0, base + delta)` — never below **70**

Verified by `test_hard_clamps` (pushes every value past its clamp).

## 7. Diagnostics added

Every analysed signal's `diagnostics` JSON now carries: `regime_adaptive_enabled`,
`market_regime`, `base_min_rr`, `effective_min_rr`,
`base_max_sl_distance_percent`, `effective_max_sl_distance_percent`,
`base_min_confidence`, `effective_min_confidence`, `regime_threshold_reason`.
The same is exposed at `GET /api/public/regime-adaptive-thresholds`.

## 8. Tests run

```
python -m compileall app tests        # clean
pytest -q tests/test_regime_adaptive_gate.py   # 8 passed
pytest -q                              # 393 passed
ruff check / black --check             # clean
docker compose build bot               # image built
```
The full suite passing with the flag **off** confirms the default path is
unchanged. The new tests cover: disabled passthrough, LOW/HIGH/SIDEWAYS
adaptation, hard clamps, unknown/None regime → NORMAL, and diagnostics.

## 9. Before / after scan summary

The threshold transformation is proven by tests + the API. The live scan-summary
comparison must be produced by enabling the flag on the running stack (the
deployed instance here is a prior image and was intentionally not restarted):

```
# enable, then watch one cycle:
REGIME_ADAPTIVE_GATE_ENABLED=true   # in .env
docker compose up -d bot
docker logs signals-bot --tail=300 | grep -A12 "SCAN SUMMARY"
# also look for the new line:  "REGIME ADAPTIVE GATE: market_regime=… min_rr 1.5 -> 1.0 …"
```

Expected for LOW_VOLATILITY (per spec; not forced):

| Metric | Before | After |
|--------|:------:|:-----:|
| Confidence pass | 1–2 | 2–5 |
| RR pass | 0 | 1–3 |
| Emitted | 0 | 0–2 |

## 10. Recommendation for live usage

Enable in **staging/observation first**: turn the flag on, watch a few cycles of
SCAN SUMMARY + the per-signal diagnostics to confirm the relaxed LOW_VOLATILITY
thresholds let *quality* setups through (not noise). The hard clamps bound the
downside (RR never < 1.0, SL never > 20%, confidence never < 70). Tune the
per-regime values from the diagnostics before relying on it in production. It is
orthogonal to the live-trading gate — it changes which signals are *emitted*,
never whether a real order is placed.

## Commit

`Add Regime Adaptive Gate for dynamic RR and SL thresholds`
