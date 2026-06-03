# Sprint 16-18 Alpha Optimization Report

**Date:** 2026-05-31  
**Version:** V9 Alpha  
**Branch:** develop  
**Status:** Complete — syntax clean, Docker build passes, all imports verified

---

## Summary

Four sprints implemented: Signal Diagnostics Engine, Auto Winrate Analyzer, Dynamic RR Engine, Liquidity Sweep Engine, and Adaptive Threshold Engine.

---

## Sprint 16A — Signal Diagnostics Engine

**Goal:** Store WHY every signal was generated.

### Files Changed

| File | Change |
|---|---|
| `app/database/models.py` | Added `diagnostics TEXT` (nullable) and `rr_method VARCHAR(32)` to `Signal` and `ArchivedSignal` |
| `app/database/session.py` | Added 4 `ALTER TABLE … ADD COLUMN IF NOT EXISTS` schema upgrades |
| `app/scanner/scanner.py` | Builds `diagnostics` JSON object at signal creation time |
| `app/main.py` | Passes `diagnostics` and `rr_method` to `create_signal()` |
| `app/dashboard/server.py` | Added `GET /api/public/diagnostics/{signal_id}` endpoint |

### Diagnostics Object Schema

```json
{
  "trend_score": 18.0,
  "structure_score": 3.0,
  "setup_score": 4.0,
  "entry_score": 4.0,
  "funding_score": 8,
  "oi_score": 6,
  "liquidity_score": 0,
  "base_confidence": 82.0,
  "total_score": 88.0,
  "rr_method": "structure",
  "funding_class": "positive",
  "tier": "VIP"
}
```

### API Endpoint

```
GET /api/public/diagnostics/{signal_id}

Response:
{
  "signal_id": 42,
  "symbol": "BTCUSDT",
  "side": "LONG",
  "confidence": 88.0,
  "rr_method": "structure",
  "diagnostics": { ... }
}
```

### Backward Compatibility

- Both new columns are `nullable` — existing signals remain fully compatible.
- `diagnostics` is `NULL` on old signals; the endpoint returns `{}` in that case.

---

## Sprint 16B — Auto Winrate Analyzer

**Goal:** Automatically learn what works across multiple dimensions.

### Files Changed

| File | Change |
|---|---|
| `app/analytics/winrate.py` | NEW — analytics service computing win rates across 6 dimensions |
| `app/dashboard/server.py` | Added `GET /api/public/winrate-analysis` endpoint |

### Dimensions Analyzed

- **Side**: LONG vs SHORT win rate
- **Confidence buckets**: 70-75, 75-80, 80-85, 85-90, 90+
- **RR buckets**: 1.5-2.0, 2.0-2.5, 2.5-3.0, 3.0+
- **Timeframe buckets**: 15m, 1h, 4h, 1d
- **Funding class** (from diagnostics JSON): positive vs negative win rate
- **OI direction** (from diagnostics JSON): rising vs falling win rate

### API Endpoint

```
GET /api/public/winrate-analysis

Response:
{
  "sample_size": 211,
  "long_winrate": 62.4,
  "short_winrate": 39.1,
  "funding_positive_winrate": 58.0,
  "funding_negative_winrate": 41.0,
  "oi_rising_winrate": 61.0,
  "oi_falling_winrate": 38.0,
  "best_confidence_bucket": "85-90",
  "best_timeframe": "15m",
  "best_rr_bucket": "2.0-2.5",
  "confidence_buckets": [...],
  "rr_buckets": [...],
  "timeframe_buckets": [...]
}
```

### Notes

- Funding/OI breakdowns require `diagnostics` data; returns `null` for pre-Sprint-16A signals.
- Analyzes last 500 closed signals by default.

---

## Sprint 16C — Dynamic RR Engine

**Goal:** Replace the fixed 2.2× RR with dynamically selected RR from three methods.

### Files Changed

| File | Change |
|---|---|
| `app/risk/levels.py` | Full rewrite — 3 RR candidates, dynamic selection, `rr_method` tracking |
| `app/scanner/scanner.py` | Passes `liq_signal` to `build_levels()`, logs `rr_method` |

### Three RR Methods

| Method | Logic | RR Range |
|---|---|---|
| `atr` | `tp2 = price + mult × risk` where `mult` scales 2.5 (low vol) → 1.8 (high vol) via ATR% | 1.8–2.5 |
| `structure` | `tp2 = recent_high` (LONG) or `recent_low` (SHORT) — 40-bar swing level | Varies |
| `liquidity` | `tp2 = eq_high_level` or `eq_low_level` from Liquidity Engine | Varies |

**Selection rule:** Evaluate all valid candidates (RR ≥ `min_rr`), pick the highest.

### TradeLevels Changes

Added `rr_method: str = "atr"` field to the `TradeLevels` dataclass.

### Performance Impact

- Eliminates the fixed 2.2× RR that showed up identically in all 211 historical signals.
- Signals in low-volatility markets can now achieve RR 2.5+.
- High-volatility signals are more conservative (1.8×) reducing overextended TP placement.

---

## Sprint 17 — Liquidity Sweep Engine

**Goal:** Detect stop hunts and fake breakouts. Optional via `ENABLE_LIQUIDITY_ENGINE=true`.

### Files Changed

| File | Change |
|---|---|
| `app/indicators/liquidity.py` | NEW — full liquidity pattern detection engine |
| `app/config.py` | Added `enable_liquidity_engine: bool = False` |
| `app/scanner/scanner.py` | Runs liquidity analysis when enabled, integrates score |

### Patterns Detected

| Pattern | Description |
|---|---|
| Equal Highs | 2+ candle highs within 0.1% tolerance (supply zone / stop cluster) |
| Equal Lows | 2+ candle lows within 0.1% tolerance (demand zone / stop cluster) |
| Sweep Up | Wick pierced above equal-highs, body closed below (bull trap) |
| Sweep Down | Wick pierced below equal-lows, body closed above (bear trap) |
| Fake Breakout Up | Prior bar closed above equal-highs, current bar reversed |
| Fake Breakout Down | Prior bar closed below equal-lows, current bar reversed |
| Stop Hunt Bull | Aggressive down-wick (>2× body) below swing low |
| Stop Hunt Bear | Aggressive up-wick (>2× body) above swing high |
| Swing Failure Bull | New high on wick, bearish close (bull trap → reversal) |
| Swing Failure Bear | New low on wick, bullish close (bear trap → reversal) |

### Score Integration

- `liquidity_score_for_side()` returns 0-20 (direction-aware: only counts patterns favoring the signal side).
- Integrated into `adjusted_confidence` with weight 0.5× (max +10 to confidence).
- Contributes to `diagnostics.liquidity_score`.

### Enabling

```env
ENABLE_LIQUIDITY_ENGINE=true
```

---

## Sprint 18 — Adaptive Threshold Engine

**Goal:** System automatically adapts `MIN_CONFIDENCE`, `ENTRY_PASS_SCORE`, `MIN_RR` based on performance.

### Files Changed

| File | Change |
|---|---|
| `app/adaptive/__init__.py` | NEW — module exports |
| `app/adaptive/engine.py` | NEW — adaptive threshold logic |
| `app/config.py` | Added `adaptive_thresholds`, `adaptive_min_trades`, `adaptive_lookback` |
| `app/scanner/scanner.py` | Calls `run_adaptive_cycle()` after each full scan |

### Logic

1. Load last `adaptive_lookback` (default: 100) closed signals
2. Skip if fewer than `adaptive_min_trades` (default: 50) trades
3. Group signals into 5-point confidence buckets, compute win rate per bucket
4. If the next higher bucket wins ≥5% more with ≥5 samples → raise threshold by 2.5
5. If current bucket wins <40% and lower bucket is better → lower threshold by 2.5
6. Separately: if high-RR signals (≥cur_rr+0.5) win ≥5% more → raise `min_rr` by 0.5

### Safety Limits

| Setting | Safe Range |
|---|---|
| `MIN_CONFIDENCE` | 70.0 – 95.0 |
| `ENTRY_PASS_SCORE` | 1 – 5 |
| `MIN_RR` | 1.5 – 4.0 |

Adapted values are stored in `system_settings` table (keys: `adaptive_min_confidence`, `adaptive_entry_pass_score`, `adaptive_min_rr`).

### Enabling

```env
ADAPTIVE_THRESHOLDS=true
ADAPTIVE_MIN_TRADES=50
ADAPTIVE_LOOKBACK=100
```

---

## Database Changes

| Table | Column | Type | Migration |
|---|---|---|---|
| `signals` | `diagnostics` | `TEXT` nullable | Auto via `session.py` startup |
| `signals` | `rr_method` | `VARCHAR(32)` nullable | Auto via `session.py` startup |
| `archive_signals` | `diagnostics` | `TEXT` nullable | Auto via `session.py` startup |
| `archive_signals` | `rr_method` | `VARCHAR(32)` nullable | Auto via `session.py` startup |

All migrations use `ADD COLUMN IF NOT EXISTS` — safe to run on existing databases.

---

## API Changes

| Endpoint | Sprint | Description |
|---|---|---|
| `GET /api/public/diagnostics/{id}` | 16A | Full diagnostics for a signal |
| `GET /api/public/winrate-analysis` | 16B | Rolling win-rate analysis |

All existing endpoints unchanged.

---

## Performance Impact

- Scanner: +1 optional async call (liquidity analysis, only if `ENABLE_LIQUIDITY_ENGINE=true`)
- Scanner: +1 async call per scan cycle for adaptive engine (only if `ADAPTIVE_THRESHOLDS=true`)
- Both features are off by default — zero overhead unless explicitly enabled
- `build_levels()` now evaluates up to 3 RR candidates instead of 1, negligible CPU cost

---

## Remaining Risks

1. **Liquidity RR in practice**: The structure-based TP2 uses `recent_high`/`recent_low` (40-bar extremes). In strong trending markets, these extremes may be far above/below entry, resulting in very high (but unrealistic) RR values. Monitor `rr_method=structure` signals.

2. **Adaptive engine cold-start**: With fewer than 50 trades, adaptation is disabled entirely. New deployments will run on static thresholds until enough data accumulates.

3. **Diagnostics storage size**: Each signal adds ~300 bytes of JSON. At 12 signals/hour, 24/7, that's ~86 KB/day. Negligible.

4. **Funding/OI breakdowns in winrate-analysis**: Return `null` until new signals (with diagnostics) accumulate. This resolves automatically over time.
