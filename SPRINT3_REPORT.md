# ALPHA RADAR SIGNALS V3.1 — Sprint 3 Report
## Signal Detail Page

**Date:** 2026-05-30  
**Sprint:** 3 — Signal Detail Page  
**Status:** ✅ COMPLETE

---

## Overview

Sprint 3 delivers a complete, polished signal detail page at `/signal/{id}`. The page renders fully server-side as static HTML with a single async JSON fetch — no additional dependencies required.

---

## Route

| Method | Path | Auth |
|--------|------|------|
| `GET` | `/signal/{id}` | Public |
| `GET` | `/api/public/signal/{id}` | Public (JSON) |

---

## What the Page Displays

### Hero Bar
- Symbol (large, prominent)
- Side badge (LONG green / SHORT red)
- Timeframe chip
- Signal ID chip
- Confidence % (top-right)

### PnL / Status Banner
- Unrealised PnL (OPEN) or Final PnL (closed)
- PnL color-coded: teal (open), green (win), red (loss)
- Status display: **OPEN** / **TP1** / **TP2** / **TP3** / **SL**
- Each status has its own colour class:
  - `OPEN` → teal
  - `TP1 / TP2 / TP3` → green
  - `SL` → red
  - `EXPIRED / CANCELLED` → yellow

### Signal Info Card
| Field | Value |
|-------|-------|
| Symbol | e.g. BTCUSDT |
| Side | LONG / SHORT badge |
| Timeframe | chip (15m) |
| Confidence | % in teal |
| Risk/Reward | 1 : 2.2 |
| Risk Level | LOW / MEDIUM / HIGH |
| Opened | 2026-05-30 14:22 |
| Closed | shown only when signal is closed |

### Levels Card
| Field | Colour |
|-------|--------|
| Entry Zone | `low → high` in teal |
| Stop Loss | red |
| TP1 | mid-green |
| TP2 | bright green |
| TP3 | light green |

### MTF Layer Scores Card
Four animated progress bars, 2×2 grid:

| Score | Range | Displayed |
|-------|-------|-----------|
| 1D Trend Score | 0 – 20 | value / 20 |
| 4H Structure | 0 – 5 | value / 5 |
| 1H Setup | 0 – 5 | value / 5 |
| 15M Entry | 0 – 10 | value / 10 |

- Bar colour shifts from dark teal → bright teal as score improves
- `N/A` shown (with zeroed bar) for legacy signals without scores

### Reasoning Card
- One item per reason, with a teal dot prefix
- All pipeline reasons recorded at signal generation time
- "No reasoning recorded" shown if field is empty

### Navigation
- `← Back to Signals` link at **top** of page (before content)
- `← Back to Signals` link at **bottom** of page (after reasoning)

---

## API Response — `/api/public/signal/{id}`

All required fields confirmed present:

```json
{
  "id": 1,
  "symbol": "OPGUSDT",
  "side": "SHORT",
  "timeframe": "15m",
  "confidence": 78.7,
  "risk_reward": 2.2,
  "risk_level": "MEDIUM",
  "strategy": "MTF_SMC_STRICT",
  "status": "TP1",
  "pnl_pct": 4.2,
  "entry_low": 0.207039,
  "entry_high": 0.207361,
  "stop_loss": 0.217515,
  "tp1": 0.194822,
  "tp2": 0.184508,
  "tp3": 0.171099,
  "trend_score": null,
  "structure_score": null,
  "setup_score": null,
  "entry_score": null,
  "reasons": ["EMA stack aligned", "Momentum confirmed", ...],
  "created_at": "2026-05-29T14:22:01+00:00",
  "closed_at": "2026-05-29T18:44:12+00:00"
}
```

`trend_score` / `structure_score` / `setup_score` / `entry_score` are `null` for signals generated before V3.1. New signals populate all four from the MTF pipeline.

---

## Files Changed

| File | Change |
|------|--------|
| `app/dashboard/server.py` | `_signal_detail_page_html()` rewritten; no other changes |

---

## Validation

### Syntax
```
app/dashboard/server.py   ✅ OK
```

### Docker Build
```
docker compose build   →   ✅ SUCCESS
```

### docker compose up -d
```
signals-postgres   ✅ Healthy
signals-redis      ✅ Healthy
signals-bot        ✅ Started — no errors
```

### Route Test
```
GET /signal/1              →   200 OK  (HTML page)
GET /api/public/signal/1   →   200 OK  (JSON, all 16 fields present)
GET /signal/99999          →   200 OK  (page shows "Signal not found")
```

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 3 Signal Detail Page*  
*Generated 2026-05-30*
