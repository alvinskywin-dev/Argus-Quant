# ALPHA RADAR SIGNALS — Duplicate Signal Guard Report

**Date:** 2026-05-30  
**Feature:** Strict duplicate prevention for active signals  
**Status:** ✅ COMPLETE — 22/22 tests pass, clean startup

---

## Root Cause

The system had a 30-minute same-direction cooldown (`CooldownTracker`) but **no check for whether the previous signal was still OPEN**. After 30 minutes, a new signal for the same symbol could be generated even if the original position had not been closed. This caused:

- Duplicate OPEN signals for the same symbol on the dashboard
- Duplicate Telegram posts on the next scan cycle
- Polluted performance statistics

---

## Guard Architecture — 3 Layers

The fix implements defense-in-depth with three independent guard layers:

```
Scanner (Layer 1)
  └─ has_active_signal(symbol)           ← blocks before any publish
       ↓ if passes
main.py (Layer 2)
  └─ has_active_signal(symbol)           ← race-condition safety before DB write
       ↓ if passes
bot.py (Layer 3 — publisher)
  └─ has_active_signal_excluding(symbol, signal_id) ← blocks duplicate Telegram posts
```

---

## Rule Set

| Condition | Action |
|-----------|--------|
| Same symbol is OPEN / ACTIVE / PENDING | **Block** new signal (layer 1+2) |
| Same symbol + same side is OPEN | **Block** (when `block_same_symbol_while_open=False`) |
| Same symbol — any side — closed less than 24h ago | **Block** (post-close cooldown) |
| Previous signal status is TP1/TP2/TP3/SL | **Allow** (after cooldown) |
| Previous signal status is CLOSED/EXPIRED/CANCELLED | **Allow** |

---

## Changes by File

### `app/config.py`

Three new settings:

```env
BLOCK_SAME_SYMBOL_WHILE_OPEN=true        # default: block ANY direction
BLOCK_SAME_SYMBOL_SIDE_WHILE_OPEN=true   # fallback: block same side only
SIGNAL_DUPLICATE_COOLDOWN_HOURS=24       # re-entry silence after TP/SL (0=off)
```

### `app/database/repo.py`

New constant and three guard query functions:

```python
ACTIVE_STATUSES = ["OPEN", "ACTIVE", "PENDING"]

async def has_active_signal(symbol, side=None) -> bool
    # True if any OPEN/ACTIVE/PENDING row exists for symbol
    # side=None → symbol-level (any direction)
    # side="LONG" / "SHORT" → direction-specific

async def has_active_signal_excluding(symbol, exclude_id) -> bool
    # Same as above but skips the signal with exclude_id
    # Used by publisher guard to not block the just-created signal

async def in_post_close_cooldown(symbol, side, hours) -> bool
    # True if a TP/SL close for symbol+side happened within `hours` hours
    # Returns False immediately when hours=0 (feature disabled)

async def get_active_signals_summary() -> list[dict]
    # Returns all active signals — used by admin dashboard
```

### `app/database/session.py`

- Schema upgrades are now **non-fatal**: individual failures are logged as warnings, not startup crashes (critical for the partial unique index which requires clean data first)
- New upgrade statement:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_signal_symbol
ON signals(symbol)
WHERE status IN ('OPEN', 'ACTIVE', 'PENDING');
```

> **Note:** This index requires no duplicate OPEN signals in the DB.  
> Run `dedup_open_signals.py` migration first if the index creation fails.

### `app/scanner/scanner.py`

Two new guard blocks inserted after the RR check, before `cooldown.mark_emitted()`:

1. **Active-signal guard** — calls `has_active_signal()`; logs `SKIP_DUPLICATE_ACTIVE_SIGNAL`
2. **Post-close cooldown** — calls `in_post_close_cooldown()`; logs `SKIP_DUPLICATE_ACTIVE_SIGNAL … reason=post_close_cooldown_24h`

New scan counters: `duplicate_active`, `post_close_cooldown`  
Scan summary line added: `Dup skipped: N`

### `app/main.py`

- **Pre-persist guard** (layer 2): checks `has_active_signal(symbol)` before `create_signal()` — catches race conditions where two scan tasks both passed layer 1
- Tags `sig["_signal_id"] = persisted.id` after DB write so layer 3 can use `has_active_signal_excluding()`

### `app/telegram_bot/bot.py`

- **Publisher guard** (layer 3): calls `has_active_signal_excluding(symbol, signal_id)` inside `broadcast_signal()` — blocks duplicate Telegram posts even if two signals were both persisted before either was broadcast

### `app/dashboard/server.py`

New endpoint:
```
GET /api/admin/active-signals   (admin auth required)
→ {"active": [...], "count": N}
```

Admin Health tab now shows:
- **Active Signals** card with count badge
- Table: Symbol · Side · Status · Confidence · Opened
- Polls every 15 seconds
- Shows `✅ No active signals — clean` when empty

### `app/database/migrations/dedup_open_signals.py` (new)

One-time migration that finds duplicate OPEN signals per symbol, keeps the most recent, and sets older ones to `EXPIRED`. Required before the partial unique index can be applied.

```bash
docker compose exec bot python -m app.database.migrations.dedup_open_signals
docker compose restart bot   # unique index will then apply cleanly
```

---

## New Configuration (`.env`)

```env
# Duplicate signal prevention (all ON by default)
BLOCK_SAME_SYMBOL_WHILE_OPEN=true
BLOCK_SAME_SYMBOL_SIDE_WHILE_OPEN=true
SIGNAL_DUPLICATE_COOLDOWN_HOURS=24
```

To disable post-close cooldown (allow re-entry immediately after TP/SL):
```env
SIGNAL_DUPLICATE_COOLDOWN_HOURS=0
```

---

## Test Results

```
tests/test_duplicate_guard.py::test_open_signal_blocks_new               PASSED
tests/test_duplicate_guard.py::test_closed_signal_allows_new             PASSED
tests/test_duplicate_guard.py::test_sl_signal_allows_new                 PASSED
tests/test_duplicate_guard.py::test_tp_signal_allows_new                 PASSED
tests/test_duplicate_guard.py::test_opposite_side_blocked_by_symbol_guard PASSED
tests/test_duplicate_guard.py::test_has_active_excluding_passes_for_own_id PASSED
tests/test_duplicate_guard.py::test_has_active_excluding_blocks_on_other_open PASSED
tests/test_duplicate_guard.py::test_post_close_cooldown_disabled_when_zero PASSED
tests/test_duplicate_guard.py::test_post_close_cooldown_active_when_recent_close PASSED
tests/test_duplicate_guard.py::test_post_close_cooldown_clear_when_no_recent_close PASSED

==================== 22 passed in 3.56s ====================
(10 new + 12 existing indicators/scoring tests)
```

---

## Validation

### Build
```
docker compose build   →   ✅ SUCCESS
```

### Startup
```
✅ Binance   OK
✅ Database  OK
✅ Redis     OK
✅ Telegram  OK
Dashboard:  :8010
```

### Log format for blocked signals
```
SKIP_DUPLICATE_ACTIVE_SIGNAL symbol=BTCUSDT side=LONG reason=existing_open_signal
SKIP_DUPLICATE_ACTIVE_SIGNAL symbol=BTCUSDT side=LONG reason=post_close_cooldown_24h
SKIP_DUPLICATE_ACTIVE_SIGNAL symbol=BTCUSDT side=LONG reason=existing_open_signal (pre-persist guard)
SKIP_DUPLICATE_ACTIVE_SIGNAL symbol=BTCUSDT side=LONG reason=existing_open_signal (publisher guard)
```

---

## Post-Deployment Checklist

```
[ ] docker compose up -d
[ ] Verify clean startup: docker compose logs bot --tail=20
[ ] Run dedup migration:  docker compose exec bot python -m app.database.migrations.dedup_open_signals
[ ] Restart bot:          docker compose restart bot
[ ] Verify unique index applied (no warning in logs)
[ ] Open admin dashboard → Health tab → confirm "Active Signals" section loads
[ ] Monitor scan logs for SKIP_DUPLICATE_ACTIVE_SIGNAL entries
[ ] Confirm no duplicate OPEN signals appear on /signals page
```

---

*ALPHA RADAR SIGNALS — Duplicate Signal Guard*  
*Generated 2026-05-30*
