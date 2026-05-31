# Sprint 20F — Binance Live Trading

**Status:** ✅ Complete · **MOCK by default — no real orders** · feature-flagged · protected V10 engines untouched
**Date:** 2026-05-31

First real exchange adapter (Binance USDT-M Futures) behind a unified adapter
interface, with full live order/position/trade persistence and audit logging.
The defining property: **no real order is ever placed unless the live-trading
gate is fully open** (`LIVE_TRADING_ENABLED=true` AND `MOCK_EXCHANGE_MODE=false`).

---

## What shipped

### Unified adapter layer `app/exchange_adapters/`
| File | Responsibility |
|------|----------------|
| `base.py` | The common interface (connect / balance / positions / open_order / close_order / set_tp_sl / set_leverage / set_margin_type / get_order_status) + result dataclasses. Every result carries `mode` = MOCK or LIVE. |
| `mock.py` | `MockExchangeAdapter` — deterministic, offline, no network. The default. |
| `binance.py` | `BinanceFuturesAdapter` — real signed REST (HMAC-SHA256) for USDT-M futures, incl. TP/SL (`STOP_MARKET`/`TAKE_PROFIT_MARKET`), trailing (`TRAILING_STOP_MARKET`), leverage, margin type, reduce-only. |
| `__init__.py` | `resolve_adapter()` — the single chokepoint that returns LIVE only when the gate is open. 20G registers OKX/Bybit/Bitget here. |

### Live execution `app/live_trading/`
- `service.py` — resolves the adapter from vaulted creds (20C), applies the safety gate (20E), executes, and records `live_orders` / `live_positions` / `live_trades` + `live_audit_log` for **every order, fill, error, and rejection**.
- `router.py` — `/api/live/{status,balance,open,close,leverage,positions,orders,trades}` (auth-gated).

### Database
`live_orders`, `live_positions`, `live_trades`, `live_audit_log` — each row carries `mode` (MOCK/LIVE) so it is unambiguous whether a real order occurred.

### The gate (defense in depth)
1. `resolve_adapter()` returns a `BinanceFuturesAdapter` **only** when `live_trading_enabled AND not mock_exchange_mode AND creds present`; otherwise a `MockExchangeAdapter`.
2. `BinanceFuturesAdapter._guard()` re-checks the same condition before *every* network call and raises otherwise.
3. The live API is only mounted when `LIVE_TRADING_API_ENABLED=true`; exposing it does **not** enable real orders.
4. Each open also passes the 20E safety gate (global/user kill, lockout).

### Safety / audit (spec checklist)
- Open / Close / Reduce-only / TP-SL / Trailing / Leverage / Margin type (isolated/cross) / position sizing — all via the adapter interface.
- Audit logging of every order, error, rejection, and fill, written in an independent session so REJECT/FAIL records survive a request rollback.

---

## Validation
- `docker compose build` — clean.
- Full suite: **139 passed** (129 prior + 10 new `tests/test_exchange_adapters.py`).
  - **Binance HMAC signature matches the documented Binance test vector** (`c8db56…6b71`) — proves the signing is correct without any network call.
  - **Gate matrix:** LIVE returned *only* when `live_enabled && !mock && creds`; MOCK in every other permutation (live-off, mock-on-with-live-on, no creds, unknown exchange). `_guard()` raises when closed.
- End-to-end vs Postgres in MOCK (`tests/e2e_live_manual.py`): status gate closed → balance → open (qty 0.1) → position → close (+$500) → 2 orders + trade + audit (OPEN/CLOSE OK, OPEN REJECTED), safety kill switch blocks an open (403). **Every result mode=MOCK; no real order placed.** Test data cleaned up.

## Config (`.env` / `.env.example`)
`LIVE_TRADING_API_ENABLED` (mount the API; default off), reuses
`LIVE_TRADING_ENABLED` + `MOCK_EXCHANGE_MODE` (execution gate) and
`BINANCE_TESTNET` (testnet base URL).

## Notes / follow-ups
- Real Binance calls are intentionally **not** exercised in CI (no network/keys); only the signing and gating are unit-tested. Going live requires a testnet/live key in the vault, `MOCK_EXCHANGE_MODE=false`, and `LIVE_TRADING_ENABLED=true`.
- 20G generalises `resolve_adapter` to OKX/Bybit/Bitget on this same interface + auto-routing.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer — no changes.
