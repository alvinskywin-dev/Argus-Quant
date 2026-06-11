# Live Safety Hardening Report

**Project:** ARGUS QUANT
**Branch:** `chore/devops-quality-foundation`
**Scope:** Capital-safety hardening of the live-execution path, derived from a
trading-standards review of the codebase. Eight findings (#1–#8) addressed.
**Status:** Local commits only — not pushed. No DB migrations. All new behaviour
is default-safe (off, or unchanged in the default MOCK mode).

---

## 1. Context

A general review of the source against professional trading-system standards
found the architecture sound (default-safe live gate, strong safety layer,
read-only reconciliation engine, defensive entry/protection ordering) but
flagged eight gaps in the live-execution path that could risk capital or
misreport performance. This report documents the remediation of all eight.

Severity at review time: #1–#4 were the highest capital risk; #5–#7 correctness
of execution; #8 reporting honesty.

---

## 2. Summary of changes

| # | Finding | Fix | Commit |
|---|---------|-----|--------|
| 1 | No idempotency key on orders → duplicate-fill risk on retry | `newClientOrderId` + lookup-by-id | `330af14` |
| 2 | Network timeout assumed = failure → orphan/duplicate | Distinct timeout error + state resolution | `330af14` |
| 3 | No periodic DB↔exchange drift detection while trading | Gated reconciliation loop + admin alert | `ab93541` |
| 4 | No slippage guard on MARKET entries | Pre-trade reject + post-fill telemetry | `c6f310b` |
| 5 | Live sizing was notional-only, not risk-based | Risk-per-trade sizing wired into live open | `c6f310b` |
| 6 | MARKET fill price assumed (avgPrice=0) | Re-read order to capture true fill | `f2b8d92` |
| 7 | No server-time sync → -1021 rejects on clock drift | Time-offset sync + one-shot retry | `f2b8d92` |
| 8 | Reported PnL/winrate were gross (no fees) | Net-of-estimated-fees reporting | `1894d06` |

Test totals: **+34 safety tests**; full suite **615 passed**; `ruff` and
`black` clean; `compileall` OK.

---

## 3. Findings in detail

### #1 — Order idempotency key
**Root cause:** `open_order` sent no `newClientOrderId`. On a retry (or after an
ambiguous timeout) the same intent could place a second order → double fill.
**Fix:** orders now carry a `client_order_id` (Binance `newClientOrderId`).
Binance rejects a duplicate id (-4015), so a blind retry can never double-fill,
and the order can be looked up by that key. Added `get_order_by_client_id`.
**Files:** `exchange_adapters/base.py`, `binance.py`, `mock.py` (idempotent
registry), `bybit/okx/bitget.py` (accept kwarg), `live_trading/service.py`.

### #2 — Timeout-safe entry
**Root cause:** a dropped connection on the order POST raised a generic error
that the caller treated as failure — but the order may already have landed.
**Fix:** `_request` now raises a distinct `AdapterTimeoutError`. `open_position`
resolves the true state via `get_order_by_client_id` (retried), adopting the
order if it landed and only reporting a clean failure when the exchange has
none. Prevents orphan positions and duplicate fills.
**Files:** `binance.py`, `live_trading/service.py`
(`_open_entry_idempotent`, `_resolve_after_timeout`).

### #3 — Periodic reconciliation
**Root cause:** the read-only reconciliation engine ran only via API and a
one-shot startup sweep; live positions could drift (manual close, liquidation,
partial fill, lost TP/SL) unnoticed while trading.
**Fix:** `reconciliation_loop()` runs `reconcile_all_active_users()` every
`RECONCILIATION_INTERVAL_SEC`. Admins are alerted only on **newly-persisted**
drift (the engine de-dupes unresolved issues), so a standing issue is not
re-alerted every cycle. Strictly read-only — never opens/closes/cancels orders.
**Files:** `reconciliation/loop.py` (new), `main.py`, `config.py`.

### #4 — Slippage guard
**Root cause:** MARKET entries executed at any price; a market that ran away
from the signal was still chased.
**Fix:** pre-trade, a MARKET entry is **rejected (409)** when the live mark has
moved more than `MAX_SLIPPAGE_BPS` adverse to the intended entry. Favourable
moves never trip it. Post-fill, realised slippage beyond the band is logged +
audited (`HIGH_SLIPPAGE`). Pure helpers in `risk/slippage.py`.
**Files:** `risk/slippage.py` (new), `live_trading/service.py`, `config.py`.

### #5 — Risk-based sizing
**Root cause:** the live open path sized by raw notional only; risk-per-trade
was not enforced even though the maths existed.
**Fix:** `open_position` can size by entry→stop distance risking `risk_pct`% of
available balance (capped by `LIVE_MAX_NOTIONAL_FRAC`). Sizing precedence:
`quantity` > `notional_usdt` > `risk_pct + stop_loss`. Exposed via the
`/live/open` API (`OpenLiveIn.risk_pct`).
**Files:** `live_trading/service.py`, `schemas.py`, `router.py`, `config.py`.

### #6 — Real fill price reconciliation
**Root cause:** a Binance MARKET POST response often returns `avgPrice=0`, so
entry basis / PnL were built on an assumed price.
**Fix:** after a MARKET fill with no avgPrice, `open_order` re-reads the order
(`get_order_status`) and merges the true `avgPrice`/`executedQty`. Best-effort:
keeps the original result if the re-read fails.
**Files:** `binance.py` (`_reconcile_fill`).

### #7 — Server-time sync
**Root cause:** signed requests used the host clock + `recvWindow` only; clock
drift caused `-1021` rejections.
**Fix:** the adapter syncs a server-time offset from `/fapi/v1/time` (cached 30
min), applies it to every signed timestamp, and on a `-1021` force-resyncs and
retries the request once.
**Files:** `binance.py` (`_sync_time`, `_ensure_time_offset`, offset-aware
`_request`).

### #8 — Net-of-fees reporting
**Root cause:** reported PnL / profit factor were gross price move (accounting
off by default), overstating real performance.
**Fix:** performance reporting subtracts an estimated round-trip taker fee
(`net_pnl_pct` / `signal_net_pnl`). Applied in the performance engine, dashboard
analytics, and daily/weekly Telegram stats. The performance API exposes
`pnl_basis` + `roundtrip_fee_bps`. Lifecycle win/loss classification is
unchanged — only PnL magnitude is netted.
**Files:** `accounting/pnl.py`, `analytics/performance.py`,
`dashboard/routes/analytics_router.py`, `daily_stats_job.py`,
`weekly_stats_job.py`, `config.py`.

---

## 4. Configuration reference

All new settings are default-safe. Defaults preserve existing behaviour except
#8, which reports net PnL by default (set `REPORT_FEES_ENABLED=false` to revert).

| Setting | Default | Purpose |
|---------|---------|---------|
| `RECONCILIATION_LOOP_ENABLED` | `false` | Enable the periodic drift sweep (#3) |
| `RECONCILIATION_INTERVAL_SEC` | `300` | Sweep cadence (min 30) |
| `RECONCILIATION_ALERT_CRITICAL` | `true` | Admin-alert on newly-found drift |
| `SLIPPAGE_GUARD_ENABLED` | `true` | Reject MARKET entry on excess slippage (#4) |
| `MAX_SLIPPAGE_BPS` | `50.0` | Adverse band vs intended entry (0.5%); 0 disables |
| `LIVE_RISK_PER_TRADE_PCT` | `1.0` | Default risk-per-trade % for risk sizing (#5) |
| `LIVE_MAX_NOTIONAL_FRAC` | `1.0` | Cap: margin ≤ this fraction of balance |
| `REPORT_FEES_ENABLED` | `true` | Report PnL net of estimated fees (#8) |
| `REPORT_ROUNDTRIP_FEE_BPS` | `8.0` | Round-trip taker fee (2 × 0.04%) |

> Idempotency (#1), timeout resolution (#2), fill reconciliation (#6) and
> server-time sync (#7) are always-on in the live adapter and need no flags.

---

## 5. Validation

```
python -m compileall app tests scripts   → OK
pytest -q                                 → 615 passed, 2 warnings
ruff check .                              → All checks passed
black --check .                           → clean (223 files)
```

The 2 warnings are pre-existing `datetime.utcnow()` deprecations in
`tests/test_auth.py`, unrelated to this work.

New test files:
- `tests/test_order_idempotency.py` — 6 (#1/#2)
- `tests/test_reconciliation_loop.py` — 7 (#3)
- `tests/test_slippage_and_sizing.py` — 9 (#4/#5)
- `tests/test_binance_execution_robustness.py` — 7 (#6/#7)
- `tests/test_net_pnl_reporting.py` — 5 (#8)

---

## 6. Deployment notes

- **No DB migration** — idempotency/timeout resolution is in-call; tp_history /
  reconciliation use existing tables/JSON.
- **No production restart** is performed by these changes.
- To activate the periodic sweep in production set
  `RECONCILIATION_LOOP_ENABLED=true` (and tune the interval).
- Live trading still requires the existing double gate
  (`LIVE_TRADING_ENABLED=true` **and** `MOCK_EXCHANGE_MODE=false`); none of these
  changes open that gate.

### Known follow-ups (not in scope here)
- **Cross-restart idempotency:** `client_order_id` is resolved in-call only;
  persisting it on `LiveOrder` (a small additive column) would make idempotency
  survive a process restart, ideally keyed deterministically per signal/intent.
- **Exact fees:** #8 uses an estimated taker fee. Enabling the accounting engine
  (`ACCOUNTING_ENABLED`) with settled `userTrades` fees + funding would make
  reported PnL exact rather than estimated.

---

## 7. Rollback

Each finding is an isolated commit (see §2) and can be reverted independently
with `git revert <hash>`. No data cleanup is required:
- #1/#2/#6/#7 are adapter-internal; reverting restores the prior request path.
- #3 is a scheduled task gated off by default; reverting just removes the loop.
- #4/#5 add optional params and a default-on guard; set the flags off or revert.
- #8 is reporting-only; `REPORT_FEES_ENABLED=false` restores gross instantly.
