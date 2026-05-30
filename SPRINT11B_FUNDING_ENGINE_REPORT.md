# Sprint 11B — Funding Rate Engine Report

## Overview

Funding rate data from Binance USDT-M Futures is now integrated as a crowd-positioning filter. Signals passing the full MTF pipeline receive a funding adjustment to their confidence score before broadcast. Crowded trades are penalised; contrarian setups are rewarded.

---

## Files Changed

| File | Change |
|---|---|
| `app/market_data/funding.py` | **New** — FundingData, classify_funding(), score_funding_for_side(), fetch_funding_rate(), fetch_funding_rates() |
| `app/database/models.py` | Added `FundingRateSnapshot` model (`funding_rate_snapshots` table) |
| `app/database/repo.py` | Added `save_funding_snapshot()`, `get_latest_funding()` |
| `app/config.py` | Added 7 funding config fields with safe defaults |
| `.env.example` | Added funding rate section |
| `app/scanner/scanner.py` | Integrated funding fetch + scoring + DB save + diagnostics |
| `app/telegram_bot/formatter.py` | Added `_funding_line()` — optional funding row on signal card |
| `app/dashboard/server.py` | Added `GET /api/funding/status` endpoint |
| `tests/test_funding.py` | **New** — 38 tests |

---

## New Environment Variables

```env
FUNDING_ENABLED=true              # Master switch (default: true)
FUNDING_CACHE_SECONDS=300         # Redis batch cache TTL in seconds
FUNDING_POSITIVE=0.0003           # Positive funding threshold
FUNDING_NEGATIVE=-0.0003          # Negative funding threshold
FUNDING_EXTREME_POSITIVE=0.0008   # Extreme positive threshold
FUNDING_EXTREME_NEGATIVE=-0.0008  # Extreme negative threshold
FUNDING_WEIGHT=10                 # Reference weight (informational)
```

All thresholds are safe-read from environment at runtime — no restart needed for threshold changes.

---

## Scoring Logic

### Classification (`classify_funding`)

| Rate Range | Classification |
|---|---|
| ≥ +0.0008 | `extreme_positive` |
| ≥ +0.0003 | `positive` |
| ≤ −0.0008 | `extreme_negative` |
| ≤ −0.0003 | `negative` |
| Otherwise | `neutral` |

### Funding Score Table

| Classification | LONG Score | SHORT Score |
|---|---|---|
| `neutral` | +5 | +5 |
| `negative` | +8 | −5 |
| `extreme_negative` | +10 | −15 |
| `positive` | −5 | +8 |
| `extreme_positive` | −15 | +10 |

### Reasoning

- **LONG + negative funding**: Shorts paying longs → contrarian bullish → boost.
- **LONG + extreme positive**: Longs are crowded, overextended → heavy penalty.
- **SHORT + positive funding**: Longs paying shorts → contrarian bearish → boost.
- **SHORT + extreme negative**: Shorts are crowded → heavy penalty.
- **Neutral**: No crowd bias → small positive nudge for both directions.

---

## Confidence Adjustment

Applied after OI score, clamped to [0, 100]:

```
adjusted_confidence = clip(base_confidence + oi_score + funding_score, 0, 100)
```

**Example signal log:**
```
💰 BTCUSDT LONG | Funding: rate=0.0910% class=extreme_positive score=-15 | Longs crowded; avoid chasing.
✅ SIGNAL  BTCUSDT LONG  tier=VIP  conf=77.0%  (base=85.0 oi=+7 funding=-15)  rr=1:2.2
```

---

## Batch Caching

`fetch_funding_rates()` fetches all symbols via `/fapi/v1/premiumIndex` (no symbol param) in one request, cached at Redis key `funding:batch` for `FUNDING_CACHE_SECONDS` (default 5 min). All 200+ symbols share the same batch response, keeping API weight near zero per scan cycle.

---

## Database Table

```sql
CREATE TABLE funding_rate_snapshots (
    id               SERIAL PRIMARY KEY,
    symbol           VARCHAR(32) NOT NULL,
    funding_rate     FLOAT NOT NULL,
    funding_time     BIGINT,
    next_funding_time BIGINT,
    classification   VARCHAR(32) DEFAULT 'neutral',
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ix_funding_snapshots_symbol_created ON funding_rate_snapshots(symbol, created_at);
```

Table is auto-created on startup via `Base.metadata.create_all()`.

---

## Dashboard API

`GET /api/funding/status` — no auth required

```json
{
  "funding_status": "active",
  "total_symbols": 45,
  "extreme_positive_funding": 3,
  "extreme_negative_funding": 1,
  "neutral_funding": 28,
  "positive_funding": 10,
  "negative_funding": 3,
  "snapshots": [...]
}
```

Cards available: **Funding Status**, **Extreme Positive Funding**, **Extreme Negative Funding**, **Neutral Funding**.

---

## Telegram Signal Card

Funding line is appended when data is present:

```
💰 Funding • 0.0910%  Extreme Positive ⚠️  Score -15
```

Clean fallback — line is omitted when funding data is unavailable. No changes to card layout or image generation.

---

## Risk Impact

| Scenario | Effect |
|---|---|
| BTC funding at 0.09% (extreme positive), LONG signal | −15 pts → may drop signal below PUBLIC tier |
| ETH funding at −0.05% (extreme negative), LONG signal | +10 pts → may boost from PUBLIC to VIP |
| Most altcoins with neutral funding | +5 pts → marginal improvement |
| Funding fetch fails / Redis down | 0 pts → confidence unchanged, signal still emits |

The funding engine is non-blocking: any fetch failure degrades gracefully to a zero score without suppressing the signal.

---

## Validation Results

```
docker build -t alpha-radar-sprint11b .  → ✅ SUCCESS

python -m py_compile \
  app/market_data/funding.py \
  app/scanner/scanner.py \
  app/ai_scoring/scorer.py \
  app/ai_scoring/mtf.py       → ✅ OK

pytest tests/ -v               → ✅ 80/80 passed
  tests/test_funding.py        → 38 passed
  tests/test_open_interest.py  → 19 passed
  tests/test_scoring.py        →  4 passed
  tests/test_indicators.py     →  8 passed
  tests/test_duplicate_guard.py→ 10 passed (existing, unaffected)
```

---

*Sprint 11B complete. No existing scanner logic, Entry Engine V2, database data, dashboard HTML, or Telegram bot commands were broken.*
