# Trade Outcome / Winrate Fix Report

**Project:** ARGUS QUANT
**Change:** Lifecycle-aware trade outcome classification
**Scope:** Analytics + display only — no signal, execution, DB-schema, or trading-engine logic changed.

---

## 1. Root cause

The signal tracker (`app/scanner/tracker.py`) stored only the **latest** status on
each `Signal` row. A trade that hit a take-profit and then drifted back to the
stop loss followed this path:

```
OPEN  →  status = "TP1"   (take-profit reached)
      →  status = "SL"    (price returned to stop — OVERWRITES "TP1")
```

Because every winrate consumer counted wins as `status in ("TP1","TP2","TP3")`
and losses as `status == "SL"`, the overwrite silently converted a real win into
a loss. The fact that TP1 had ever been reached was lost entirely.

## 2. Old behavior

| Lifecycle            | Stored status | Counted as |
|----------------------|---------------|------------|
| Entry → TP1 → SL     | `SL`          | ❌ LOSS    |
| Entry → TP2 → SL     | `SL`          | ❌ LOSS    |
| Entry → TP3          | `TP3`         | ✅ WIN     |
| Entry → SL (no TP)   | `SL`          | ✅ LOSS    |

Winrate was understated by every TP-then-SL trade.

## 3. New lifecycle-aware behavior

TP history is now persisted into `Signal.diagnostics.tp_history` as each event
fires, so a later SL can no longer erase a take-profit. Outcome is derived from
the **highest take-profit ever reached**, not the last status.

| Lifecycle            | Stored status | tp_history.max_tp_hit | Outcome      | Winrate bucket |
|----------------------|---------------|-----------------------|--------------|----------------|
| Entry → TP1 → SL     | `SL`          | 1                     | PARTIAL_WIN  | WIN            |
| Entry → TP2 → SL     | `SL`          | 2                     | WIN          | WIN            |
| Entry → TP3          | `TP3`         | 3                     | FULL_WIN     | WIN            |
| Entry → SL (no TP)   | `SL`          | 0                     | LOSS         | LOSS           |
| Manual close (+pnl)  | `CLOSED`      | 0                     | WIN          | WIN            |
| Manual close (−pnl)  | `CLOSED`      | 0                     | LOSS         | LOSS           |
| Break-even           | `BE`          | 0                     | BREAKEVEN    | BREAKEVEN      |
| Still running        | `OPEN`        | 0                     | OPEN         | OPEN (excluded)|

## 4. Outcome rules

Implemented in `app/analytics/trade_outcome.py` →
`classify_trade_outcome(status, tp1_hit_at, tp2_hit_at, tp3_hit_at, sl_hit_at, realized_pnl, diagnostics)`
returning a `TradeOutcome(outcome, winrate_bucket, max_tp_hit, first_exit_event,
final_exit_event, realized_pnl, reason)`.

Precedence (a lifecycle win beats a later stop-out):

1. `status` OPEN/ACTIVE/PENDING with no exits → **OPEN**
2. TP3 ever reached → **FULL_WIN**
3. TP2 ever reached → **WIN**
4. TP1 ever reached → **PARTIAL_WIN**
5. SL hit and no TP ever reached → **LOSS**
6. Explicit break-even → **BREAKEVEN**
7. Otherwise (manual / generic close) → sign of `realized_pnl`:
   `> 0` WIN, `== 0` BREAKEVEN, `< 0` LOSS

`max_tp_hit` is derived from TP timestamps, the latest status, **and** any
persisted `tp_history.max_tp_hit` — so both new and legacy rows classify
correctly.

Winrate buckets: `PARTIAL_WIN / WIN / FULL_WIN → WIN`; `BREAKEVEN → BREAKEVEN`;
`LOSS → LOSS`; `OPEN → OPEN`.

## 5. Winrate formula

```
wins   = PARTIAL_WIN + WIN + FULL_WIN
losses = LOSS
winrate = wins / (wins + losses)         # break-even and open excluded
```

`app/analytics/winrate.py` also now reports, alongside the existing breakdowns,
an `outcome_summary`:
`overall_winrate, wins, losses, partial_win_count, full_win_count, win_count,
partial_win_rate, full_win_rate, breakeven_count, tp1_then_sl_count,
tp2_then_sl_count`.

## 6. Telegram message changes

`TelegramBot.broadcast_event` (`app/telegram_bot/bot.py`) now frames a stop hit
according to TP history instead of always saying "STOP LOSS":

| Event           | Message |
|-----------------|---------|
| TP2/TP3 → SL    | `🟢 WIN LOCKED • SYMBOL SIDE` |
| TP1 → SL        | `🟡 PARTIAL WIN • SYMBOL SIDE` |
| SL (no TP)      | `🛑 STOP LOSS • SYMBOL SIDE` |
| TP1/TP2/TP3 hit | `🎯 TPx HIT • SYMBOL SIDE` |

The holding-time line (`⏱ 2h 14m in trade`) and `⚡ ARGUS QUANT` branding are
preserved. The tracker fan-out payload now carries `trade_outcome`,
`winrate_bucket`, and `max_tp_hit`.

## 7. Dashboard changes

`app/dashboard/routes/analytics_router.py`:
- All winrate numerators/denominators now use lifecycle-aware `is_win()` /
  `is_loss()` instead of raw status.
- A new `outcome_distribution` block (FULL_WIN / WIN / PARTIAL_WIN / BREAKEVEN /
  LOSS / OPEN) is returned next to the raw `status_distribution`, so the UI can
  render PARTIAL WIN / WIN / FULL WIN / LOSS / BREAKEVEN badges and no longer
  shows a TP1→SL trade as a plain SL in performance analytics.

The same lifecycle helpers were threaded through `app/analytics/performance.py`,
`app/database/repo.py::winrate_summary`, `app/daily_stats_job.py`, and
`app/weekly_stats_job.py`.

## 8. Tests run

New: `tests/test_trade_outcome_classification.py` (13 tests) covering all 10
required cases — SL-before-TP, TP1→SL, TP2→SL, TP3, manual profit/loss,
break-even, OPEN exclusion, legacy TP statuses, legacy SL + tp_history, plus
`record_exit_event` accumulation.

```
pytest -q tests/test_trade_outcome_classification.py   → 13 passed
pytest -q                                              → 581 passed, 2 warnings
python -m compileall app tests scripts                 → OK
ruff check .                                           → All checks passed
black --check .                                        → 216 files unchanged
docker compose build bot                               → Image built (exit 0)
```
(The 2 warnings are pre-existing `datetime.utcnow()` deprecations in
`tests/test_auth.py`, unrelated to this change.)

## 9. Backfill instructions

`scripts/backfill_trade_outcomes.py` is **non-destructive** and dry-run by
default. It writes only `diagnostics.tp_history` (never status/PnL/timestamps).
For legacy SL rows where the TP hit was erased, it recovers `max_tp_hit` from
`max_favorable_pct` vs the TP levels.

```bash
# preview (writes nothing)
python -m scripts.backfill_trade_outcomes
python -m scripts.backfill_trade_outcomes --days 90

# apply
python -m scripts.backfill_trade_outcomes --apply

# inside the container
docker compose exec bot python -m scripts.backfill_trade_outcomes --apply
```

Going forward the tracker records tp_history live, so the backfill only needs to
run once against historical rows.

## 10. Rollback plan

Pure code change, no DB migration:
- **Revert code:** `git revert <commit>` (or check out the previous commit).
  Winrate reverts to status-only counting immediately; no data cleanup needed.
- **tp_history is additive:** the `diagnostics.tp_history` blocks written by the
  tracker/backfill are ignored by the old code path, so leaving them in place is
  harmless after a rollback.
- **No production restart is performed by this change.** Deploy/restart is a
  manual operator step.
