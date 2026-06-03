# Sprint 19 — Market Regime Engine & Short Protection Layer

## Overview

Two complementary systems designed to address the SHORT winrate problem (26.3% vs LONG 53.3%):

- **Sprint 19A**: Market Regime Engine classifies overall market conditions before signals are scored
- **Sprint 19B**: Short Protection Layer applies 5 sequential filters to reject low-quality SHORT setups

---

## Files Changed

### New Files

| File | Purpose |
|------|---------|
| `app/market_data/market_regime.py` | Market Regime Engine — calculates and caches BULL/BEAR/SIDEWAYS/HV/LV classification |
| `app/scanner/short_protection.py` | Short Protection Layer — 5 filters, stat tracking, analytics helper |

### Modified Files

| File | Change |
|------|--------|
| `app/database/models.py` | Added `market_regime VARCHAR(32)` and `regime_score INTEGER` to Signal and ArchivedSignal |
| `app/database/session.py` | Added 4 `ALTER TABLE … ADD COLUMN IF NOT EXISTS` upgrade statements |
| `app/scanner/scanner.py` | Integrated regime scoring, secondary confidence gate, short protection, updated scan summary |
| `app/dashboard/server.py` | Added `GET /api/public/market-regime` and `GET /api/public/short-protection` endpoints |

---

## API Changes

### GET /api/public/market-regime

Returns the current market regime classification and supporting metrics.

```json
{
  "market_regime": "BULL",
  "regime_score": 82,
  "breadth": 68.2,
  "breadth_ema50": 62.5,
  "btc_trend": "UP",
  "eth_trend": "UP",
  "atr_percentile": 45.0,
  "calculated_at": "2026-05-31T12:00:00"
}
```

Returns HTTP 503 if the regime has not been calculated yet (before the first scan cycle).

### GET /api/public/short-protection

Returns aggregated short protection filter statistics (rolling 7-day window, Redis-backed).

```json
{
  "short_candidates": 120,
  "short_rejected": 77,
  "rejection_rate": 64.1,
  "top_reason": "Bull regime",
  "reasons": {
    "Bull regime": 41,
    "Funding": 12,
    "OI": 8,
    "Liquidity": 10,
    "Trend mismatch": 6
  }
}
```

---

## Signal Impact

### Confidence Adjustments (regime delta applied after OI + funding scoring)

| Regime | LONG | SHORT |
|--------|------|-------|
| BULL | +5 | -10 |
| BEAR | -10 | +5 |
| SIDEWAYS | -5 | -5 |
| HIGH_VOLATILITY | 0 | 0 |
| LOW_VOLATILITY | 0 | 0 |

Signals whose adjusted confidence drops below `min_confidence` after regime adjustment are rejected at the new secondary confidence gate.

### SHORT Protection Filters

| # | Filter | Trigger | Action |
|---|--------|---------|--------|
| 1 | Bull Regime | BULL + conf < min_confidence + 8 | Reject |
| 2 | Funding | Positive funding + price > EMA200 | Reject |
| 3 | Open Interest | Price rising + OI rising | Reject |
| 4 | Liquidity | No bearish sweep + liquidity_score < 8 | Reject |
| 5 | Trend Alignment | 15m or 1h not bearish | Reject |

### Diagnostics Fields Added to Each Signal

```json
{
  "market_regime": "BULL",
  "regime_score": 82,
  "regime_delta": 5,
  "short_protection_pass": true,
  "short_rejection_reason": null
}
```

### DB Columns Added to `signals` and `archive_signals`

```sql
ALTER TABLE signals ADD COLUMN IF NOT EXISTS market_regime VARCHAR(32);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS regime_score INTEGER;
```

Existing signals retain NULL values for these columns — no data migration needed.

---

## Market Regime Engine — Technical Design

**Regime classification inputs:**

| Input | Weight | Source |
|-------|--------|--------|
| BTC 1D trend (vs EMA200) | ±12 pts | Binance 1D klines |
| ETH 1D trend (vs EMA200) | ±8 pts | Binance 1D klines |
| BTC 4H trend (vs EMA200) | ±6 pts | Binance 4H klines |
| ETH 4H trend (vs EMA200) | ±4 pts | Binance 4H klines |
| BTC 1H trend (vs EMA200) | ±3 pts | Binance 1H klines |
| Breadth EMA200 >70% | +10 pts | Top 50 USDT pairs 1D |
| Breadth EMA200 <30% | -10 pts | Top 50 USDT pairs 1D |
| BTC ATR percentile >80 | Override → HIGH_VOLATILITY | BTC 1D ATR vs 90-bar history |
| BTC ATR percentile <20 | Override → LOW_VOLATILITY | BTC 1D ATR vs 90-bar history |

**Regime thresholds:** Score ≥ 62 = BULL, ≤ 38 = BEAR, 39–61 = SIDEWAYS

**Caching:** Redis with 10-minute TTL. `ensure_regime_fresh()` is called once per scan cycle before symbol analysis begins. Per-symbol calls hit the Redis cache (no extra API calls).

---

## Performance Impact

**Expected changes:**
- SHORT signal volume: significant decrease (~40-70% reduction depending on market conditions)
- LONG signal volume: unchanged
- SHORT winrate: expected improvement as low-quality setups are filtered before emission
- LONG winrate: minor improvement from regime adjustments boosting/reducing confidence appropriately

**Resource overhead:**
- ~7 additional Binance API calls per scan cycle (BTC/ETH klines across 3 timeframes + ATR)
- Breadth calculation: 50 × 1D kline fetches — heavily cached (3600s Redis TTL), minimal cost after warm-up
- All regime data cached in Redis for 10 minutes — zero additional cost per symbol during a cycle

---

## Remaining Risks

1. **Breadth accuracy**: Using top 50 symbols by volume is a proxy for full market breadth. The estimate may diverge from true breadth during altcoin-specific moves that don't affect BTC/ETH.

2. **Regime lag**: 10-minute cache TTL means regime classification can be up to 10 minutes stale. In fast-moving markets, the regime may not reflect a rapid trend reversal immediately.

3. **SHORT volume collapse**: In a sustained BULL regime with positive funding, nearly all SHORT candidates will be rejected. This is intentional but means fewer actionable SHORT signals even in valid setups.

4. **HIGH/LOW_VOLATILITY scoring impact**: The spec calls for "require higher structure score" (HIGH_VOLATILITY) and "require stronger liquidity score" (LOW_VOLATILITY) rather than a direct confidence delta. These are not yet implemented as hard gates — they are classified and stored but the enforcement logic is deferred to a future sprint.

5. **Cold start**: On container restart, the regime cache is empty until the first scan cycle runs `ensure_regime_fresh()`. The `/api/public/market-regime` endpoint returns HTTP 503 until then (~30 seconds after startup).
