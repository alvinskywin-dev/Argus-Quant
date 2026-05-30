# ALPHA RADAR SIGNALS V3.1 — Enterprise Cleanup Upgrade Report

**Date:** 2026-05-30  
**Version:** V3.1 Enterprise Cleanup  
**Commit:** ALPHA RADAR SIGNALS V3.1 Enterprise Cleanup

---

## Summary

This report documents the V3.1 Enterprise Cleanup upgrade. All 10 phases have been implemented and validated.

---

## Phase 1 — Legacy Data Cleanup ✅

**Problem:** Dashboard showed legacy 5m signals (confidence 55–76%, RR 1:2.2) from the old engine.

**Solution:**
- Added `archive_signals` table (`ArchivedSignal` model in `app/database/models.py`)
- Created `app/database/migrations/archive_legacy.py` — moves all `timeframe='5m'` and `strategy != 'MTF_SMC_STRICT'` signals into archive (preserving data, not deleting)
- Dashboard now only displays 15m / 1H / 4H / 1D signals from the MTF pipeline

**Run once to clean existing database:**
```bash
docker compose exec bot python -m app.database.migrations.archive_legacy
```

---

## Phase 2 — Performance Recalculation ✅

**Problem:** Stats were polluted by legacy signals skewing win rate, profit factor, and signal counts.

**Solution:**
- Created `scripts/rebuild_performance.py`
- Recomputes `DailyStat` rows using only `strategy='MTF_SMC_STRICT'` signals
- Reports: Win Rate, Avg PnL, Profit Factor, Sharpe Ratio

**Run once after Phase 1:**
```bash
docker compose exec bot python scripts/rebuild_performance.py
```

---

## Phase 3 — Signal Detail Page ✅

**New route:** `GET /signal/{id}`

**New API:** `GET /api/public/signal/{id}`

- Shows: symbol, side, timeframe, entry, stop loss, take profits (TP1/TP2/TP3), confidence, RR
- **MTF Layer Scores** (new columns added to `signals` table):
  - `trend_score` — 1D trend gate score (10–20)
  - `structure_score` — 4H structure hits (0–5)
  - `setup_score` — 1H setup hits (0–5)
  - `entry_score` — 15M entry score (0–10)
- Full reasoning list from the pipeline
- Visual score bars for each layer

**DB migration:** Columns are nullable — existing signals show N/A for scores; new signals populate all four.

---

## Phase 4 — Performance Center ✅

**Route:** `GET /performance` (existing, enhanced)

Additions to `GET /api/public/performance`:
- `avg_hold_min` — average hold time in minutes (computed from `closed_at - created_at`)
- `best_symbols` — top 5 symbols by average PnL%
- `worst_symbols` — bottom 5 symbols by average PnL%

Existing fields retained: Win Rate, Profit Factor, Avg RR, Total Closed, LONG/SHORT breakdown, Monthly table, Leaderboard.

---

## Phase 5 — Health Center ✅

**Route:** `GET /health` — now returns a full HTML health dashboard

**Displays:**
- Overall status (ONLINE / DEGRADED)
- Uptime
- Last signal time
- Universe size
- Per-component status cards:
  - Dashboard (always ONLINE if page loads)
  - Database (latency in ms)
  - Redis (latency in ms)
  - Binance WebSocket (price feed health)
  - Telegram (configured indicator)
  - Scanner (scan interval)
- Configuration table: min confidence, min RR, scan interval, max signals/hr, paper trading

Machine-readable JSON health check remains at `GET /api/health`.

---

## Phase 6 — Paper Trading ✅

**Route:** `GET /paper`

**API:** `GET /api/public/paper`

**Features:**
- Virtual 10 000 USDT starting balance
- 1% risk per trade (100 USDT position size base)
- Open positions table (all OPEN MTF signals)
- Closed trades table (all closed MTF signals, most recent first)
- Running balance calculated by applying each trade's `pnl_pct` to 1% of current balance
- Win rate, total PnL in USDT, current balance

**Database model added:** `PaperPosition` (for future automated paper trading integration)

---

## Phase 7 — Backtest Engine ✅

**Route:** `GET /backtest`

**API:** `GET /api/public/backtest`

**Metrics computed from all closed MTF signals:**
- Win Rate
- Profit Factor (gross wins / gross losses)
- Max Drawdown %
- Sharpe Ratio (simplified: avg_pnl / std_pnl)
- Average RR
- RR Distribution (bar chart)
- Monthly breakdown table
- Cumulative PnL equity curve (bar visualization)

---

## Phase 8 — Entry Engine Fix ✅

**Status: Already implemented in V3.0 MTF refactor.**

The old hard-BOS requirement was replaced with a score-based entry system in `app/ai_scoring/mtf.py`:

| Trigger       | Points |
|---------------|--------|
| BOS           | +2     |
| FVG retest    | +2     |
| OB retest     | +2     |
| EMA pullback  | +1     |
| VWAP reclaim  | +1     |
| Momentum      | +1     |
| Vol spike >50% | +1    |

Minimum entry score: **2 points** (configurable via `ENTRY_MIN_SCORE`). Any combination of the above triggers can satisfy the entry gate.

---

## Phase 9 — Public Landing Page ✅

**Route:** `GET /`

Added inline **FAQ section** to the homepage (before the affiliates section) with 8 Q&A cards covering:
- Free signals policy
- How to receive signals
- Markets covered
- Confidence score explanation
- 4-layer MTF pipeline explanation
- Risk/Reward explanation
- Position sizing guidance
- Project identity / disclaimer

Navigation bar in all subpages updated to include: Signals, Performance, Paper, Backtest, Health, FAQ.

---

## Phase 10 — Final Validation ✅

### Build

```
docker compose build  →  SUCCESS (no errors, no warnings)
```

### Syntax Checks

```
python3 -m py_compile app/database/models.py        →  OK
python3 -m py_compile app/ai_scoring/mtf.py         →  OK
python3 -m py_compile app/scanner/scanner.py        →  OK
python3 -m py_compile app/main.py                   →  OK
python3 -m py_compile app/dashboard/server.py       →  OK
python3 -m py_compile app/database/migrations/archive_legacy.py  →  OK
python3 -m py_compile scripts/rebuild_performance.py →  OK
```

### No Import Errors

All modules import cleanly inside the Docker build context.

---

## Files Changed

| File | Change |
|------|--------|
| `app/database/models.py` | + `trend_score`, `structure_score`, `setup_score`, `entry_score` nullable cols on `Signal`; + `ArchivedSignal` model; + `PaperPosition` model |
| `app/ai_scoring/mtf.py` | + `trend_score`, `structure_score`, `setup_score`, `entry_score_pts` fields on `MTFDecision`; pipeline populates them |
| `app/scanner/scanner.py` | Passes layer scores in signal dict |
| `app/main.py` | Persists layer scores to DB when saving signals |
| `app/dashboard/server.py` | + `/signal/{id}`, `/paper`, `/backtest` routes; `/health` now HTML; performance API enhanced; new HTML generators; FAQ section in homepage; nav updated |

## New Files Created

| File | Purpose |
|------|---------|
| `app/database/migrations/__init__.py` | Package init |
| `app/database/migrations/archive_legacy.py` | One-time archive migration (Phase 1) |
| `scripts/rebuild_performance.py` | Performance recalculation (Phase 2) |
| `V31_UPGRADE_REPORT.md` | This report |

---

## Post-Deployment Checklist

1. Run `docker compose up -d`
2. Verify dashboard loads at `http://localhost:8010`
3. Run archive migration: `docker compose exec bot python -m app.database.migrations.archive_legacy`
4. Run performance rebuild: `docker compose exec bot python scripts/rebuild_performance.py`
5. Confirm `/health` shows all components
6. Confirm `/signals` shows only 15m/1h/4h/1d signals
7. Confirm `/paper` loads virtual portfolio
8. Confirm `/backtest` shows metrics
9. Spot-check `/signal/{id}` for a known signal ID

---

*ALPHA RADAR SIGNALS V3.1 — Enterprise Cleanup*  
*Generated 2026-05-30*
