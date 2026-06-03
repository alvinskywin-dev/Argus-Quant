# Sprint 21 — Live Safety Foundation Report

**Date:** 2026-06-01
**Branch:** `sprint-21-live-safety` (off `develop`)
**Scope:** Live-trading safety foundations only. No changes to the Signal Engine,
Scanner, Strategy logic, Risk scoring, or Market-regime logic.

> ⚠️ **LIVE-GATE WARNING.** This deployment's `.env` currently has
> `LIVE_TRADING_ENABLED=true` and `MOCK_EXCHANGE_MODE=false`, so the execution
> gate is **OPEN** (`/api/live/status` → `"mode":"LIVE"`). Real orders are
> therefore *possible* for any user with connected, validated exchange keys. At
> the time of this report **0 users have connected exchange API keys**, so no
> real order can fire yet. **All tests in this sprint use MOCK / pure logic — no
> real orders were placed during testing.** To return to the safe default, set
> `LIVE_TRADING_ENABLED=false` and `MOCK_EXCHANGE_MODE=true` and restart.

---

## 1. Executive Summary

Sprint 21 builds the missing safety layer required before any real-money live
trading: **real API-key permission validation, DB↔exchange reconciliation,
crash/restart recovery, order-failure classification + bounded retries, net-PnL
accounting, TP/SL synchronisation, partial-failure handling, and reduce-only
emergency close.**

Every engine is **read-only or non-destructive** by design and **feature-flagged
off by default**. The only component that can place orders is *protective* (TP/SL
retry, reduce-only emergency close) and additionally requires the live gate.
The safety-critical decision logic of every engine is implemented as **pure
functions** and covered by unit tests.

- **6 new feature flags** (all default `false`).
- **6 new tables**, **10 new columns**, **11 new indexes** — all via
  non-destructive `ADD COLUMN IF NOT EXISTS` / `CREATE ... IF NOT EXISTS`.
- **233 tests pass** (89 new across 6 files), MOCK/pure only.
- Build + migrate + restart verified; all new endpoints return valid JSON.

---

## 2. What was implemented

### 21A — Exchange real permission validator (`app/exchange_vault/permission_validator.py`)
- Unified `ExchangePermissionResult` (exchange, ok, status, can_read, can_trade,
  can_futures, can_withdraw, account_type, permissions, error_code,
  error_message, permission_warning, raw_safe_summary).
- Pure classifiers for **Binance / OKX / Bybit / Bitget** + real read-only signed
  validators (account/permission endpoints only — **never an order**).
- Status states: `CONNECTED / INVALID / VALIDATION_UNAVAILABLE /
  PERMISSION_DENIED / IP_RESTRICTED / ERROR`.
- Rejects withdrawal-enabled and invalid keys; when withdraw cannot be detected
  (OKX/Bitget) returns `can_withdraw = null` **and warns** (never assumes safe).
- Vault `connect` stores a key as `CONNECTED` **only** when validation passes;
  `test` persists the full status range. No secret ever appears in logs/responses.

### 21B — Execution reconciliation engine (`app/reconciliation/`)
- Pure `reconcile_symbol` detects: DB-only / exchange-only positions, size/entry/
  leverage/margin-mode/side mismatches, and TP/SL drift (both directions).
- Orchestration `reconcile_user / reconcile_exchange_account /
  reconcile_all_active_users` — **strictly read-only** (`get_positions`,
  `get_open_orders`), records `ReconciliationIssue` rows, never opens/closes/cancels.

### 21C — Position recovery engine + TP/SL sync (`app/recovery/`)
- `recover_user_positions / recover_all_positions`: imports orphan exchange
  positions as `RECOVERED + requires_review`, marks vanished DB positions
  `CLOSED_UNKNOWN`, re-secures TP/SL. **Opens nothing.**
- `sync_tp_sl_for_position`: retries only the *missing* protective leg up to
  `TP_SL_RETRY_MAX`; otherwise marks the position `UNSAFE` + alerts admin +
  records a reconciliation issue.
- Pure `tp_sl` status: `SYNCED / MISSING_TP / MISSING_SL / MISSING_BOTH /
  UNKNOWN / UNSAFE`.
- One-shot **startup sweep** wired into boot (no-op unless flag on).

### 21D — Order failure / retry engine (`app/order_failures/`)
- Pure `classify_error` → `INSUFFICIENT_BALANCE / PRECISION_ERROR / MIN_NOTIONAL /
  RATE_LIMIT / NETWORK_TIMEOUT / EXCHANGE_DOWN / ORDER_REJECTED /
  REDUCE_ONLY_REJECTED / TP_SL_FAILED / UNKNOWN`.
- `decide_retry`: exp backoff for timeouts/exchange-down, recommended-delay for
  rate limits, precision-retry-once, terminal reject for min-notional/balance,
  and **reconcile-before-retry** for timeout/reduce-only/unknown (never blindly
  resend an order of unknown state).
- Idempotency key (no duplicate live entry per signal) + per-user circuit breaker.

### 21E — Net PnL accounting engine (`app/accounting/`)
- Pure `compute_net_pnl` = gross − commission − funding − slippage, with `net_roe`
  and `estimate_quality` (`EXACT / PARTIAL / ESTIMATED`).
- Per-trade breakdown + daily rollup; raw fee/funding events are append-only;
  **MOCK and LIVE kept separate**.

### TP/SL sync after restart / partial failure
- `open_position` now persists intended TP/SL and is **partial-failure aware**:
  if the entry fills but TP/SL placement fails, the position is **kept** and
  flagged `UNSAFE / requires_review` (never lost), a `TP_SL_FAILED` failure is
  recorded, and recovery/admin can retry or emergency-close.

### Emergency close (`app/live_trading/service.emergency_close_position`)
- Reduce-only forced close: authorise (owner/admin) → reconcile (read true
  exchange size) → cancel stale TP/SL → reduce-only market close → finalise +
  accounting + alert. **Never opens an opposite position.** Requires exact phrase
  `"CLOSE UNSAFE POSITION"` and `EMERGENCY_CLOSE_ENABLED`.

---

## 3. Files changed

41 files, +3286 / −64 (vs `develop`). New packages: `app/reconciliation/`,
`app/recovery/`, `app/order_failures/`, `app/accounting/`, plus
`app/exchange_vault/permission_validator.py`. Modified: `app/config.py`,
`app/database/{models,session}.py`, `app/exchange_adapters/{base,binance}.py`,
`app/exchange_vault/{service,router,schemas}.py`,
`app/live_trading/{service,router,schemas}.py`, `app/dashboard/server.py`,
`app/main.py`. 6 new test files + 1 test made env-independent.

---

## 4. New APIs

| Method | Path | Auth | Flag |
|---|---|---|---|
| GET | `/api/reconciliation/status` | public aggregate | `RECONCILIATION_ENABLED` |
| GET | `/api/reconciliation/issues` | user | " |
| POST | `/api/reconciliation/run` | user | " |
| GET | `/api/recovery/status` | public aggregate | `POSITION_RECOVERY_ENABLED` |
| POST | `/api/recovery/run` | user | " |
| GET | `/api/order-failures` | public aggregate | `ORDER_FAILURE_ENGINE_ENABLED` |
| GET | `/api/order-failures/list` | user | " |
| GET | `/api/order-failures/{id}` | user (owner) | " |
| POST | `/api/order-failures/{id}/retry` | user (owner) | " |
| POST | `/api/order-failures/{id}/mark-resolved` | user (owner) | " |
| GET | `/api/accounting/summary` | public aggregate | `ACCOUNTING_ENABLED` |
| GET | `/api/accounting/daily` | user | " |
| GET | `/api/accounting/trades` | user | " |
| GET | `/api/accounting/user/{id}` | user/admin | " |
| POST | `/api/live/positions/{id}/emergency-close` | user/admin + phrase | `EMERGENCY_CLOSE_ENABLED` |
| GET | `/api/saas-admin/safety-overview` | admin cookie | — |

`/api/exchange/connect` & `/test` responses gained `can_read`,
`last_validation_status`, `permission_warning`.

> Note: `*/status`, `*/summary`, and `/api/order-failures` return **non-PII
> aggregates** (counts + flags) without auth, matching the existing public
> `/api/live/status`. Per-row / per-user data requires auth.

---

## 5. New DB tables / fields

**New tables:** `reconciliation_issues`, `order_failures`,
`live_trade_accounting`, `daily_user_pnl`, `exchange_fee_events`,
`funding_fee_events` (created by `Base.metadata.create_all`).

**New columns (idempotent `ADD COLUMN IF NOT EXISTS`):**
- `exchange_accounts`: `can_read`, `last_validation_status`, `permission_warning`
- `live_positions`: `take_profit`, `stop_loss`, `tp_sl_status`,
  `requires_review`, `unsafe_reason`, `recovered_at`, `last_reconciled_at`

**New indexes (11):** on `live_positions(user_id|exchange|status)`,
`live_orders(exchange_order_id|status)`, `reconciliation_issues(user_id|resolved)`,
`order_failures(user_id|final_state)`, `live_trade_accounting(user_id)`,
`daily_user_pnl(user_id)`.

All applied non-destructively at startup (verified in Postgres). No `DROP`, no
destructive `ALTER`.

---

## 6. Safety guarantees

1. **Validation never trades.** Permission checks call read-only endpoints only.
2. **Reconciliation never mutates.** It reads and records issues; it cannot
   open/close/cancel.
3. **Recovery opens nothing.** It only imports state and places *protective*
   reduce-only TP/SL.
4. **No order is ever lost.** Entry fills are persisted before protection is
   attempted; a failed protection leaves the position `UNSAFE`, not orphaned.
5. **No blind retries.** Timeouts / reduce-only / unknown failures require
   reconciliation before any further action.
6. **No duplicate entries.** Idempotency key per signal/user/exchange/symbol/side.
7. **Emergency close is reduce-only + confirmation-gated** and reconciles first;
   it can never flip into an opposite position.
8. **No secrets leak.** Encrypted at rest; never in logs or responses.
9. **Flags default off; defaults are MOCK.** Adapters simulate unless the gate
   is explicitly open.

---

## 7. Live gate behaviour

`live_gate_open() == (LIVE_TRADING_ENABLED and not MOCK_EXCHANGE_MODE)`.
- Closed → `resolve_adapter` returns the MOCK adapter; real adapters also
  self-guard and refuse to place orders.
- Open (current state) → real adapters place real orders **only** for users with
  a `CONNECTED` (validated) key. Currently 0 such users.
- Every order/position/trade row is tagged `mode = MOCK | LIVE`; accounting keeps
  them separate.

---

## 8. Tests run

- `python -m compileall app tests` → clean.
- `pytest -q` (inside `signals-bot` container): **233 passed**.
  - New: `test_exchange_permission_validator.py` (17),
    `test_reconciliation_engine.py` (11), `test_tp_sl_sync.py` (10),
    `test_position_recovery.py` (5), `test_order_failure_engine.py` (21),
    `test_accounting_engine.py` (10), `test_emergency_close.py` (5).
- Coverage of the required cases: invalid key rejected; no-trade/no-futures
  rejected; withdrawal rejected when detectable; DB-position-missing-on-exchange;
  exchange-position-missing-in-DB; TP/SL missing after restart; entry-success +
  TP/SL-failure → UNSAFE; retry policy (all classes); timeout → reconcile-first;
  emergency close reduce-only; net-PnL calculation.
- Endpoints verified live (all return valid JSON); admin platform + users API
  still 200/data (no regression).

All tests use MOCK adapters or pure functions; **no network and no real orders.**

---

## 9. Known limitations

- **Real validator network paths are not exercised in CI** (no live keys); only
  the response→result classifiers are unit-tested. They should be validated
  against each exchange's testnet before real use.
- **`get_open_orders` / `cancel_all_orders` are implemented for Binance**; OKX/
  Bybit/Bitget inherit safe no-op defaults, so exchange-side TP/SL drift and
  emergency-close order-cancellation are Binance-only until those are added.
- **Funding fees default to 0** and commission/slippage are *estimated* unless
  the exchange supplies settled values → breakdown flagged `PARTIAL/ESTIMATED`.
- Retries and the circuit breaker **record decisions**; an always-on background
  worker that executes scheduled retries is not part of this foundation.
- Admin visibility is a **JSON endpoint** (`/api/saas-admin/safety-overview`);
  no UI redesign was done (per scope).

---

## 10. Rollback plan

- **Code:** `git checkout develop` (the sprint lives entirely on
  `sprint-21-live-safety`; nothing merged). Or revert commits
  `21A..integration`.
- **Flags (fastest):** set `RECONCILIATION_ENABLED / POSITION_RECOVERY_ENABLED /
  ORDER_FAILURE_ENGINE_ENABLED / ACCOUNTING_ENABLED / EMERGENCY_CLOSE_ENABLED =
  false` and restart → all new engines/APIs go dorment; core trading untouched.
- **DB:** migrations are additive only — leaving the new tables/columns in place
  is harmless. No destructive change to roll back.
- **Live gate:** independent of this sprint; set `LIVE_TRADING_ENABLED=false`,
  `MOCK_EXCHANGE_MODE=true` to re-close at any time.

---

## 11. Is the system safe for a small (20–50 USDT) live test?

**Foundation: YES — with prerequisites.** The safety net (validation,
reconciliation, recovery, partial-failure handling, accounting, emergency close)
is in place, default-safe, and tested. Before a real 20–50 USDT test:

1. **Validate the real validators against exchange testnets** (the network paths
   are not CI-tested).
2. **Connect one trade-only, withdrawal-disabled key** and confirm
   `/api/exchange/connect` returns `CONNECTED` with `can_withdraw=false`.
3. **Verify per-user safety limits** (Sprint 20E) and set a tight
   `order_failure_breaker_threshold`.
4. Keep `EMERGENCY_CLOSE_ENABLED=true` and confirm the reduce-only close on the
   testnet first.
5. Prefer Binance for the first live test (full `get_open_orders` /
   `cancel_all_orders` support).

With those done, a tightly-capped 20–50 USDT Binance test is reasonable. Until
the testnet validation of the real network paths is complete, **do not** rely on
this for unattended multi-exchange live trading.
