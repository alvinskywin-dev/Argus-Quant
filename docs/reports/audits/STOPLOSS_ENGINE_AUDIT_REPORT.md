# Stop-Loss Engine — Audit Report

**Date:** 2026-06-03
**Scope:** read-only audit. **No trading logic changed.**
**Code inspected:** `app/risk/levels.py`, `app/risk/filters.py`, `app/scanner/scanner.py`,
`app/scanner/tracker.py`, `app/scanner/short_protection.py`, `app/strategies/features.py`,
`app/market_data/market_regime.py`, `app/analytics/winrate.py`, `app/analytics/performance.py`,
`app/database/models.py`.
**Data:** live `signals` table (`signals-postgres`), **97 signals / 86 closed**, window
**2026-05-29 → 2026-06-02**. (Note: `app/entry_engine/` does not exist; entry/SL levels are built
in `app/risk/levels.py::build_levels`.)

> ⚠️ Small sample (86 closed, 4 days) and a single market regime in the window (LOW_VOLATILITY).
> Treat magnitudes as directional, not definitive. The structural/code findings hold regardless.

---

## 0. How stop_loss is calculated (exact flow)

`scanner._analyze_symbol` → `risk.build_levels(snap_15m, side, liq_signal)` → stored as
`Signal.stop_loss`. Tracker (`scanner/tracker.py`) polls mid-price every 30s and marks `SL` on touch.

From `app/risk/levels.py::build_levels` (price = 15m `last_close`, `atr = max(atr_value, price*0.001)`):

```python
# LONG
swing_sl = snap.recent_low  - 0.2 * atr     # recent_low = 40-bar low
atr_sl   = price            - 1.8 * atr
sl       = min(swing_sl, atr_sl)            # the LOWER → the WIDER stop
risk     = price - sl

# SHORT
swing_sl = snap.recent_high + 0.2 * atr     # recent_high = 40-bar high
atr_sl   = price            + 1.8 * atr
sl       = max(swing_sl, atr_sl)            # the HIGHER → the WIDER stop
risk     = sl - price
```

Entry zone = `price ± 0.15·ATR`; TP1 = 1.2·risk, TP3 = 3.5·risk; TP2/RR chosen dynamically
(`atr` / `structure` / `liquidity`, best valid RR ≥ `min_rr=2.0`). The SL itself does **not** depend
on regime, RR method, funding, OI, or volatility percentile.

---

## 1. SL method: ATR / structure / swing / fixed %

**Hybrid "widest-of" stop = max distance of {structure swing, fixed ATR multiple}.**

- **Structure/swing component:** 40-bar `recent_low`/`recent_high` ± `0.2·ATR` buffer.
- **ATR component:** fixed `1.8·ATR` from `last_close`.
- Combined so the **wider** of the two is used (`min` for LONG, `max` for SHORT).
- **Not** a fixed-percent stop, and **not** regime/volatility-adaptive (the 1.8 multiple and 0.2
  buffer are constants).
- Only the 15m timeframe ATR/pivots feed the SL; higher TFs gate entry but never widen the stop.

---

## 2. Average SL distance %

`sl_dist% = |entry_mid − stop_loss| / entry_mid · 100`, `entry_mid = (entry_low+entry_high)/2`.

| Side | n | Avg SL dist | Min | Max |
|------|---|-------------|-----|-----|
| ALL  | 97 | **6.66%** | 0.18% | 23.39% |
| LONG | 55 | **7.42%** | 0.18% | 20.75% |
| SHORT| 42 | **5.65%** | 1.53% | 23.39% |

**Red flag:** enormous dispersion — from **0.18%** (pure noise width, certain stop-out) to **23%**
(margin-destroying on leverage). No minimum floor and no maximum cap. The `0.2·ATR` structure buffer
collapses to near-zero whenever `recent_low/high` sits right next to price (tight ranges), producing
sub-1% stops.

---

## 3. Average time to SL

| Outcome | n | Avg hours | Median hours |
|---------|---|-----------|--------------|
| To SL   | 54 | **24.6 h** | **17.3 h** |
| To win (TP3) | 21 | 34.9 h | 28.6 h |

Losses resolve faster than wins. There is **no time-based invalidation/expiry** — `EXPIRED` exists in
the model comment but the tracker never sets it; trades sit open until TP3 or SL.

> Caveat: the tracker only writes `closed_at` on **TP3** and **SL** (not TP1/TP2), so "to win"
> covers TP3-only and undercounts partial wins.

---

## 4. Winrate by SL-distance bucket

| SL distance | closed | wins | winrate |
|-------------|--------|------|---------|
| 0–2%  | 7  | 1  | **14.3%** |
| 2–3%  | 12 | 4  | 33.3% |
| 3–4%  | 6  | 2  | 33.3% |
| 4–6%  | 27 | 8  | 29.6% |
| 6–10% | 19 | 10 | **52.6%** |
| 10%+  | 15 | 7  | 46.7% |

**Strongest finding in the audit:** winrate rises monotonically with stop width up to the 6–10% band.
Stops **< 4%** win ~**28%**; stops **≥ 6%** win ~**50%**. Tight stops are being shaken out by noise
before the thesis plays out — exactly what the 0.18%–2% stops show (14% WR).

---

## 5. Winrate by rr_method

| rr_method (column) | closed | wins | winrate | avg RR |
|--------------------|--------|------|---------|--------|
| atr   | 21 | 9  | 42.9% | 2.37 |
| (null)| 65 | 23 | 35.4% | 2.20 |

Every populated `rr_method` is **`atr`**; **`structure` and `liquidity` were never selected** in this
window. 65/86 rows have a NULL `rr_method` (older rows pre-instrumentation). The dynamic-RR engine is
effectively running ATR-only in practice → TP placement is volatility-scaled but never anchored to
real structure/liquidity, while the SL is the *wider* structural stop → asymmetry.

---

## 6. Winrate by market_regime

| regime | closed | wins | winrate |
|--------|--------|------|---------|
| (`market_regime` column) | 86 | 32 | — *(100% NULL)* |
| LOW_VOLATILITY *(from `diagnostics` JSON)* | 19 | 8 | 42.1% |

**Instrumentation bug:** `Signal.market_regime` and `Signal.regime_score` are **NULL on every row**,
even though `scanner._analyze_symbol` builds them. The values survive only inside the `diagnostics`
JSON, and only for the 21 most-recent signals. Consequence: **regime-based winrate cannot be tracked
from the dedicated columns**, and `analytics/winrate.py` has no regime dimension at all. In the
diagnostics-bearing subset the market was entirely **LOW_VOLATILITY**, so no BULL/BEAR comparison is
possible from data — Q7's BEAR portion is answered from code below.

---

## 7. Why many LONG signals hit SL in BEAR / LOW-VOL regime

Data (diagnostics subset, all LOW_VOLATILITY, all LONG): **SL rate 57.9%** (11/19),
`regime_delta = 0` on every one, avg SL dist 5.2%. Root causes:

1. **No symmetric "long protection."** `short_protection.py` aggressively filters SHORTs (5 filters),
   but **nothing guards LONGs** against adverse regimes. A LONG in a downtrend faces no structural veto.
2. **BEAR only applies a soft confidence penalty, not a block.** In `scanner.py`,
   `regime_delta = +5 / −10` (BEAR→LONG = −10) is a *score nudge*; a high-base-confidence counter-trend
   LONG still emits and then rides the trend down into SL.
3. **LOW_VOLATILITY applies ZERO adjustment.** The regime→delta map handles only BULL/BEAR/SIDEWAYS;
   `HIGH_VOLATILITY`/`LOW_VOLATILITY` fall through with `regime_delta = 0` (confirmed: every LOW_VOL
   LONG has `regime_delta=0`). Low-vol chop is mean-reverting, but directional LONGs pass unfiltered.
4. **The SL is too tight for low vol.** `atr_sl = price − 1.8·ATR` shrinks with ATR; in low vol the
   1.8·ATR stop (and the 0.2·ATR structure buffer) land inside normal noise/spread. Bucket data: the
   0–2% stops win 14%. Mid-price, touch-based SL detection compounds the sensitivity.
5. **15m-only stop under a higher-TF trend.** SL is derived purely from 15m ATR/pivots; a 15m-tight
   stop has poor survival probability when the 1D/4H trend is against the position.
6. **No break-even / no trail / no time stop.** Once price wanders ~17h (median), the full initial
   risk is given back at SL instead of being protected after favorable movement.

Net: counter-trend (or chop) LONGs are admitted with no veto and protected by a noise-width stop —
a structural recipe for the observed SL rate.

---

## 8. Recommended safer SL rules (proposals only — not implemented)

**A. Floor + cap the stop distance.**
`sl_dist = clamp(computed, min_floor, max_cap)`, e.g. `min_floor = max(0.8%, 1.2×ATR%, ~3×spread)` and
`max_cap ≈ 8–10%`. Eliminates the 0.18%–2% noise stops (14% WR) and the 23% account-killers.

**B. Regime-adaptive ATR multiple.** Replace the constant `1.8` with a regime/percentile function:
wider in LOW_VOLATILITY and chop, tighter (with smaller size) in HIGH_VOLATILITY. Data argues for a
baseline nearer **2.2–2.5×ATR** (≥6% stops win ~50% vs ~28% under 4%) — then re-validate RR/TP so RR
stays ≥ `min_rr`.

**C. Add a LONG-protection layer** symmetric to `short_protection`: hard-reject counter-trend LONGs in
BEAR; in LOW_VOLATILITY require trend alignment + a real trigger (sweep/structure) rather than a bare
score.

**D. Give LOW_/HIGH_VOLATILITY a real `regime_delta`** (currently 0) so volatility regimes actually
gate confidence instead of passing through.

**E. Anchor SL to structure, TP to structure too.** Today SL is the *wider structural* stop while TP
is usually ATR — asymmetric. Prefer swing-anchored SL **with** an ATR buffer and structure/liquidity TP,
keeping RR ≥ min.

**F. Active trade management:** move to break-even after +1R (or TP1), take partials at TP1, then trail;
add a **time-based invalidation** (the unused `EXPIRED` status) so stale trades don't sit ~17h to a full SL.

**G. Fix instrumentation before trusting regime stats:**
- Persist `market_regime`/`regime_score` (and consistently `rr_method`) on the `Signal` row — currently
  100% NULL despite being computed.
- Extend `analytics/winrate.py` with **SL-distance buckets, rr_method, market_regime**, and a
  **time-to-SL** metric; set `closed_at` on TP1/TP2 as well so win-timing is complete.

**H. Entry/risk consistency:** model risk from the actual entry mid (and optional slippage), since
`risk` is measured from `last_close` while fills occur across `entry_low..entry_high`.

---

## Appendix — queries used
All figures reproduced via read-only `psql` against `signals-postgres` (`signals` DB), e.g.:
```sql
-- winrate by SL-distance bucket
WITH c AS (SELECT *, abs((entry_low+entry_high)/2 - stop_loss)/((entry_low+entry_high)/2)*100 sld
           FROM signals WHERE status IN ('TP1','TP2','TP3','SL'))
SELECT width_bucket(sld, ARRAY[2,3,4,6,10]) b, count(*),
       count(*) FILTER (WHERE status<>'SL') wins FROM c GROUP BY 1 ORDER BY 1;
```
(Full set of queries: winrate by side, SL distance by side, time-to-outcome, SL-distance buckets,
rr_method, market_regime via column and `diagnostics` JSON, and LONG-in-LOW_VOL SL rate.)
