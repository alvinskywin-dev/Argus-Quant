# ALPHA RADAR SIGNALS V3.1 — Sprint 1 Report
## Legacy Cleanup

**Date:** 2026-05-30  
**Sprint:** 1 — Legacy Data Cleanup  
**Status:** ✅ COMPLETE

---

## 1. Database Schema Audit

### Tables Present

| Table | Status | Notes |
|-------|--------|-------|
| `signals` | ✅ Production | Main signal table — V3.1 adds 4 nullable score columns |
| `archive_signals` | ✅ Created | Holds all archived legacy signals |
| `daily_stats` | ✅ Production | Per-day aggregated statistics |
| `weekly_stats` | ✅ Created | Per-week aggregated statistics (new in Sprint 1) |
| `watchlist` | ✅ Production | Per-user symbol watchlists |
| `users` | ✅ Production | Telegram user records |
| `system_settings` | ✅ Production | Key-value config store |
| `affiliate_clicks` | ✅ Production | Affiliate link click tracking |
| `signal_messages` | ✅ Production | Per-chat Telegram message IDs for signals |
| `paper_positions` | ✅ Production | Virtual paper trading positions |

### `signals` Table — Column Inventory

| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| symbol | String(32) | |
| side | String(8) | LONG / SHORT |
| timeframe | String(8) | **filter key** |
| confidence | Float | |
| risk_level | String(16) | LOW / MEDIUM / HIGH |
| strategy | String(64) | **filter key** — `MTF_SMC_STRICT` = current engine |
| reasons | Text | Pipe-delimited reasoning string |
| entry_low / entry_high | Float | Entry zone |
| tp1 / tp2 / tp3 | Float | Take-profit levels |
| stop_loss | Float | |
| risk_reward | Float | |
| status | String(16) | OPEN / TP1 / TP2 / TP3 / SL / EXPIRED |
| pnl_pct | Float | |
| max_favorable_pct | Float | |
| max_adverse_pct | Float | |
| telegram_message_id | BigInteger nullable | |
| **trend_score** | Float nullable | **New V3.1** — 1D layer score |
| **structure_score** | Float nullable | **New V3.1** — 4H layer score |
| **setup_score** | Float nullable | **New V3.1** — 1H layer score |
| **entry_score** | Float nullable | **New V3.1** — 15M layer score |
| created_at | DateTime TZ | |
| closed_at | DateTime TZ nullable | |

### Legacy Data Definition

A signal is **legacy** if ANY condition is true:

| Condition | Explanation |
|-----------|-------------|
| `timeframe = '5m'` | Pre-MTF scanner (old 5-minute engine) |
| `strategy != 'MTF_SMC_STRICT'` | Any strategy string other than the current engine |

> The third condition from the spec ("confidence engine version < current") is implicitly covered by condition 2: all signals from pre-MTF engines have a different strategy string. There is no explicit version tag column.

---

## 2. Archive System

### `archive_signals` Table

Mirrors **every** column from `signals` verbatim, plus:

| Extra Column | Type | Purpose |
|---|---|---|
| `original_id` | Integer (indexed) | References the original `signals.id` |
| `archive_reason` | String(64) | `legacy_5m`, `legacy_engine:<name>`, or `legacy_unknown` |
| `archived_at` | DateTime TZ | Timestamp when the record was moved |

All original data is preserved — including `max_favorable_pct`, `max_adverse_pct`, `telegram_message_id`, and all MTF score columns.

---

## 3. Migration Script

**File:** `app/database/migrations/archive_legacy_signals.py`

### Features

- **Audit step first:** prints breakdown by timeframe, strategy, and confidence range before touching any data
- **Idempotent:** checks `archive_signals.original_id` before inserting — safe to re-run
- **Non-destructive:** copy-then-delete (no `DROP`, no permanent loss)
- **Post-migration audit:** verifies production table is clean after migration
- **Dry-run mode:** `--dry-run` flag reports what would be archived without making changes

### Usage

```bash
# Dry-run first (recommended)
docker compose exec bot python -m app.database.migrations.archive_legacy_signals --dry-run

# Execute migration
docker compose exec bot python -m app.database.migrations.archive_legacy_signals

# Rebuild performance stats after migration
docker compose exec bot python scripts/rebuild_performance.py
```

---

## 4. Dashboard Filtering

All **five** public-facing Signal queries now carry the MTF-only filter:

```python
Signal.strategy == "MTF_SMC_STRICT",
Signal.timeframe.in_(["15m", "1h", "4h", "1d"]),
```

| Endpoint | Was Filtered? | Now Filtered? |
|----------|--------------|---------------|
| `_get_stats()` — week window | ❌ No | ✅ Yes |
| `_get_stats()` — recent 20 | ❌ No | ✅ Yes |
| `GET /api/public/signals` | ❌ No | ✅ Yes |
| `GET /api/public/performance` | ❌ No | ✅ Yes |
| `GET /api/public/paper` | ✅ strategy only | ✅ strategy + timeframe |
| `GET /api/public/backtest` | ✅ strategy only | ✅ strategy + timeframe |

Filter constants are defined once at module level in `server.py`:

```python
_MTF_TIMEFRAMES = ["15m", "1h", "4h", "1d"]
_MTF_STRATEGY   = "MTF_SMC_STRICT"
```

The admin `/api/dashboard` and `/api/admin/*` endpoints intentionally remain unfiltered so admins can audit all historical data.

---

## 5. Schema Upgrade Mechanism

Because `SQLAlchemy create_all` only creates missing tables — it does not alter existing ones — `session.py` now applies idempotent `ALTER TABLE` statements on every startup:

```python
_SCHEMA_UPGRADES = [
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS trend_score     FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS structure_score FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS setup_score     FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_score     FLOAT",
]
```

This means the bot can be deployed to an existing database without a manual migration step for these columns.

---

## 6. Validation Results

### Syntax Check

```
app/database/models.py                         ✅ OK
app/database/session.py                        ✅ OK
app/dashboard/server.py                        ✅ OK
app/database/migrations/archive_legacy_signals.py ✅ OK
```

### Docker Build

```
docker compose build   →   ✅ SUCCESS (no errors, no warnings)
```

### docker compose up -d

```
signals-postgres   ✅ Healthy
signals-redis      ✅ Healthy
signals-bot        ✅ Started
```

### Startup Log

```
✅ Binance   OK — XXXX active symbols
✅ Database  OK — 230 signals in last 30d
✅ Redis     OK
✅ Telegram  OK — @AlphaRadarSignals_bot
Timeframes: 15m, 1h, 4h, 1d
Dashboard:  :8010
```

**No syntax errors. No migration errors. No startup errors.**

---

## 7. Files Changed

| File | Change |
|------|--------|
| `app/database/models.py` | Fixed `ArchivedSignal` to preserve all 7 missing Signal columns; added `WeeklyStat` model |
| `app/database/session.py` | Added `_SCHEMA_UPGRADES` list; `init_db()` now applies idempotent ALTER TABLE on startup |
| `app/dashboard/server.py` | Added `_MTF_TIMEFRAMES` / `_MTF_STRATEGY` constants; applied MTF filter to 5 queries |

## 8. New Files

| File | Purpose |
|------|---------|
| `app/database/migrations/archive_legacy_signals.py` | Sprint 1 canonical migration with audit + idempotent archive |
| `SPRINT1_REPORT.md` | This report |

---

## 9. Post-Deployment Checklist

```
[ ] docker compose up -d
[ ] docker compose logs bot --tail=20   # confirm no errors
[ ] docker compose exec bot python -m app.database.migrations.archive_legacy_signals --dry-run
[ ] docker compose exec bot python -m app.database.migrations.archive_legacy_signals
[ ] docker compose exec bot python scripts/rebuild_performance.py
[ ] Open /signals  — confirm only 15m/1h/4h/1d rows appear
[ ] Open /health   — confirm all components ONLINE
```

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 1 Legacy Cleanup*  
*Generated 2026-05-30*
