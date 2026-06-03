# Sprint 20D — Auto Trading Engine (DEMO MODE ONLY)

**Status:** ✅ Complete · feature-flagged (`AUTO_TRADE_DEMO_ENABLED`) · **paper accounts only, no real orders** · protected V10 engines untouched
**Date:** 2026-05-31

Executes signals automatically against per-user **paper** accounts (Sprint 20B).
No real orders are ever placed — `LIVE_TRADING_ENABLED` is irrelevant to this
engine. Implements the spec flow end to end:

```
Signal → Risk Check → Position Size → Leverage → Paper Order
       → SL/TP → Position Tracking (break-even / trailing) → Close → Statistics
```

---

## What shipped

### New package `app/auto_engine/`
| File | Responsibility |
|------|----------------|
| `risk.py` | Pure risk-check + protection maths (allow/deny, leverage clamp, trailing/break-even stop calc). Fully unit-tested. |
| `service.py` | `AutoTradeConfig` CRUD + `AutoTradeExecution` audit log + idempotency check. |
| `engine.py` | `on_new_signal()` (open for eligible users) and `on_signal_event()` (manage TP/SL) + `status()`. |
| `schemas.py` / `router.py` | `/api/auto/*` config & status API (auth-gated). |
| `__init__.py` | `setup_auto_engine(app)` — idempotent mount. |

### Database
- `auto_trade_configs` — per-user **Settings**: `enabled` (Auto Trade ON/OFF), `max_positions`, `max_leverage` (Allowed Leverage), `risk_per_trade_pct`, `allowed_exchanges`, `allowed_coins`, `min_confidence`, `order_type`, plus protection (`use_break_even`, `break_even_trigger`, `use_trailing_stop`, `trailing_distance_pct`).
- `auto_trade_executions` — audit/statistics row per decision: OPEN / SKIP / BREAK_EVEN / TRAIL / CLOSE with reason + detail.
- `paper_account_positions` gained `auto_managed` + `protection` columns (added via the idempotent `_SCHEMA_UPGRADES` migration, since the table predates this sprint).

### Engine behaviour
- **New signal:** for each opted-in user (config `enabled` **or** paper `auto_follow`), run the risk check, then open a paper position via the 20B engine's risk-based sizing at the configured leverage. Idempotent per (user, signal). Every decision is logged.
- **Risk check** (Settings honored): disabled, min-confidence, allowed-coins, allowed-exchanges, max-positions, available-margin; leverage clamped to `max_leverage`.
- **Position tracking** on TP/SL events:
  - `TP1`/`TP2` → **break-even** (move stop to entry on the configured trigger) and/or **trailing stop** (tighten behind the hit target; only ever moves favourably).
  - `TP3` → close at TP3.
  - `SL` → close at the *current* stop — which may be the break-even/trailed level, so protection actually changes the outcome.
- **Statistics:** `/api/auto/status` (opened/closed/skipped, open auto positions) + `/api/auto/executions` history.

### Integration (no protected engines touched)
Two guarded hooks in the orchestrator `app/main.py`:
- `_handle_signal` → `auto_engine.on_new_signal(id)` (after the legacy paper open).
- `_handle_tracker_event` → `auto_engine.on_signal_event(...)` (alongside the legacy paper update).
Both wrapped in `try/except` and gated by `AUTO_TRADE_DEMO_ENABLED`; they never block broadcasting or signal persistence.

---

## Validation
- `docker compose build` — clean.
- Full suite: **125 passed** (114 prior + 11 new `tests/test_auto_engine.py`: risk gates, leverage clamp, trailing/tighten maths).
- Route-mount check: `/api/auto/config|status|executions` registered.
- End-to-end vs Postgres (`tests/e2e_auto_manual.py`):
  - ETH signal **SKIPPED** by `allowed_coins=BTC`; BTC signal **opened** 1 demo position (auto_managed); re-run **idempotent** (0).
  - `TP1` → stop moved 98 → **100 (entry, break-even)**.
  - `SL` → closed at break-even stop → **PnL = $0**, not the −$100 the raw SL would have cost; balance unchanged at 10,000.
  - Executions log contains OPEN / BREAK_EVEN / CLOSE / SKIP.
  - Test data cleaned up.

## Safety
- DEMO ONLY: all execution is on paper accounts via `app.paper_engine`. No exchange adapter is called; `LIVE_TRADING_ENABLED` stays false and is not consulted here.
- Distinct flag `AUTO_TRADE_DEMO_ENABLED` — **not** the hard-locked `AUTO_TRADING_ENABLED` (which remains forced false for LIVE).

## Notes / follow-ups
- `order_type` LIMIT is stored but the demo fills as MARKET at signal entry; a resting-limit matcher is a later tracker pass.
- Daily/weekly loss limits and kill-switch are **Sprint 20E** (Safety Layer) and will wrap the risk check.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer · legacy global paper engine — no changes.
