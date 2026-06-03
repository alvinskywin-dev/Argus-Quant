# Sprint 21F — Binance Live/Testnet Validation Report

**Date:** 2026-06-02
**Branch:** `sprint-21f-binance-validation` (off `develop`, after the Sprint 21 foundation)
**Scope:** Make Binance key validation work on **testnet**, and add a strictly
read-only **preflight** that proves a key is usable end-to-end before any
real-money test. No changes to the Signal Engine, Scanner, Strategy, Risk
scoring, or Market-regime logic. **No order is ever placed by anything in this
sprint.**

> Closes the #1 prerequisite from the Sprint 21 report §9 / §11.1: *"Real
> validator network paths are not exercised in CI… validate the real validators
> against exchange testnets before real use."*

---

## 1. Why this sprint

The Sprint 21 foundation shipped a permission **classifier** but its only network
validator (`validate_binance`) hardcoded the **production** SAPI endpoint
`https://api.binance.com/sapi/v1/account/apiRestrictions`. That path:

1. **ignored `BINANCE_TESTNET`**, and
2. **does not exist on the futures testnet** (`testnet.binancefuture.com` exposes
   `fapi` but no SAPI), so a testnet key literally could not be validated.

There was also no way — before risking real money — to confirm that a connected
key can authenticate, that the host clock is in sync (signed requests fail with
`-1021` otherwise), or that the bot knows a symbol's lot/tick/min-notional
filters (needed to avoid `PRECISION_ERROR` / `MIN_NOTIONAL` rejections at order
time).

---

## 2. What was implemented

### 21F-a — Testnet-aware Binance validator (`app/exchange_vault/permission_validator.py`)
- `validate_binance(api_key, api_secret, *, testnet=None)` now honors
  `BINANCE_TESTNET` (or an explicit override):
  - **Production** → unchanged SAPI `apiRestrictions` path (authoritative
    API-key permission view, incl. the key-level withdrawal flag we reject on).
  - **Testnet** → read-only `GET /fapi/v2/account` on `testnet.binancefuture.com`.
- New pure classifier `classify_binance_futures_account(account, *, testnet, trust_withdraw_flag)`:
  - Proves **trade permission via `canTrade`** (a read-only key reports
    `canTrade=false` → `PERMISSION_DENIED`; we never infer trade rights from a
    mere balance read).
  - Treats the account-level `canWithdraw` as **undetectable** (`None` + warning)
    by default — it is not the API-key withdrawal permission, so trusting it
    could falsely reject a trade-only key. (`trust_withdraw_flag=True` opts in.)

### 21F-b — Read-only preflight + pure helpers (`app/exchange_vault/binance_preflight.py`)
Pure, unit-tested decision functions:
- `classify_clock_skew(local, server)` → `OK / WARN / FAIL` (fails before the
  `-1021` recvWindow breach).
- `parse_symbol_filters(exchangeInfo, symbol)` → `SymbolFilters` (step/tick/
  min-qty/min-notional/precision; accepts futures `notional` **and** spot
  `minNotional`).
- `round_step_down` / `round_step_up` / `round_price` (Decimal-based, no float
  drift) and `check_min_notional`.
- `plan_order_quantity(filters, price, target_notional)` → a Binance-valid qty
  for a target USDT notional (rounds to step, bumps to min-qty / min-notional).
  This is the practical bridge from "risk ~25 USDT" to a placeable order.
- `BinancePreflightResult` aggregation (`build_preflight_summary`, `finalize`,
  `to_public_dict`).

Network runner `run_binance_preflight(...)` — **read-only**: `GET /fapi/v1/time`,
signed `GET /fapi/v2/balance`, `GET /fapi/v1/exchangeInfo`, signed
`GET /fapi/v2/positionRisk`. Defaults to testnet; never opens/cancels/modifies.

### 21F-c — Flag, service, endpoint
- New flag `BINANCE_PREFLIGHT_ENABLED` (default **false**), grouped with the
  Sprint 21 flags in `app/config.py`.
- `service.binance_preflight(...)` resolves the user's vaulted Binance key and
  runs the preflight, writing an audit row. Raises no orders.
- `POST /api/live/binance/preflight?symbol=&testnet=` (auth required, returns
  `404` when the flag is off).

### 21F-d — Manual testnet runner (`scripts/binance_testnet_preflight.py`)
CLI that runs the real (read-only) validator + preflight against testnet (or
`--prod`), prints each check as JSON, and can optionally print a `--plan-notional`
order quantity. This is the operator tool for the §11.1 testnet check.

### 21F-e — Executor precision rounding (`app/exchange_adapters/binance.py`)
`open_order` now rounds qty down to `step_size` and LIMIT price to `tick_size`
via the pure `enforce_order_precision`, backed by a per-instance `exchangeInfo`
cache (one extra read-only call per symbol). A qty that rounds below `min_qty`
is rejected with a clear `AdapterError` instead of an opaque exchange reject. It
does not auto-bump size to meet min-notional (risk stays as intended).

---

## 3. Files changed

New: `app/exchange_vault/binance_preflight.py`,
`tests/test_binance_preflight.py`, `scripts/binance_testnet_preflight.py`,
this report. Modified: `app/exchange_vault/permission_validator.py`,
`app/config.py`, `app/live_trading/service.py`, `app/live_trading/router.py`,
`app/exchange_adapters/binance.py`.

---

## 4. Tests

- **259 passed** (was 233 in Sprint 21; **+26 new** in
  `test_binance_preflight.py`), run in an ephemeral container from the
  `futures-signal-bot-bot:latest` image with the repo mounted — the live
  `signals-bot` container was **not** touched.
- New coverage: clock-skew OK/WARN/FAIL; filter parsing (futures + spot
  min-notional keys, missing symbol); step-down/step-up/price rounding;
  min-notional; order-quantity planning (round, bump-to-min, unknown symbol,
  non-positive inputs); executor precision enforcement (round qty/price,
  market leaves price, below-min reject, no-filter passthrough); preflight
  aggregation (pass, hard-fail, empty); host selection; and the testnet
  futures-account classifier (canTrade true/false, unreachable, trusted-withdraw
  rejection).
- All pure / offline. The `run_binance_preflight` and `validate_binance` network
  paths remain **not exercised in CI** — run the manual script against testnet
  to exercise them.

---

## 5. Safety guarantees (unchanged + new)

1. **Validation never trades** — testnet path is a signed *read* of
   `/fapi/v2/account`; prod path is SAPI `apiRestrictions`. No order endpoint.
2. **Preflight never trades** — only `time`, `balance`, `exchangeInfo`,
   `positionRisk` (read-only). No open/close/cancel anywhere in 21F.
3. **No false trade-permission** — testnet `CONNECTED` requires `canTrade=true`.
4. **No false-safe withdraw** — account-level `canWithdraw` is reported as
   undetectable (`None`) with a warning, not trusted as the key permission.
5. **Flag default off; live gate untouched** — `BINANCE_PREFLIGHT_ENABLED=false`
   by default; the endpoint 404s when off; the preflight is independent of and
   does not alter `LIVE_TRADING_ENABLED` / `MOCK_EXCHANGE_MODE`.
6. **No secrets leak** — keys come from the vault; `to_public_dict()` never
   contains a secret.

---

## 6. How to run the testnet check (operator)

1. Create futures **testnet** keys at https://testnet.binancefuture.com.
2. `BINANCE_TESTNET=true`, set `BINANCE_API_KEY` / `BINANCE_API_SECRET`.
3. `python -m scripts.binance_testnet_preflight --symbol BTCUSDT --plan-notional 25`
4. Expect: permission `CONNECTED` (with the withdraw-undetectable warning),
   preflight `ok=true` (clock OK, account read, filters found, positions read),
   and a valid order qty for ~25 USDT.
5. Only after that passes on testnet, consider the tightly-capped 20–50 USDT
   **production** test from Sprint 21 §11.

---

## 7. Known limitations

- Preflight/validator network paths still aren't in CI (no live keys) — by
  design; the manual script is the gate.
- Testnet cannot introspect the **API-key** withdrawal permission (only the prod
  SAPI path can); on testnet it stays `None` + warning.
- The live executor now rounds: `BinanceFuturesAdapter.open_order` rounds qty
  **down** to `step_size` and LIMIT price to `tick_size` via the pure
  `enforce_order_precision` (per-instance `exchangeInfo` cache), and rejects with
  a clear error if the rounded qty falls below `min_qty`. It deliberately does
  **not** auto-bump to min-notional (that would silently increase risk);
  below-minimum notional is left to surface as a normal exchange rejection
  (classified by the 21D engine). `plan_order_quantity` remains the
  preview/planning helper for "risk ~N USDT".

---

## 8. Rollback

- **Flag:** `BINANCE_PREFLIGHT_ENABLED=false` (default) → endpoint 404s, nothing
  else changes.
- **Code:** revert this sprint's commit; the prod `validate_binance` behavior is
  byte-for-byte the original when `testnet=false`.
