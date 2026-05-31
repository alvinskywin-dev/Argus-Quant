# Sprint 20G — Multi-Exchange Adapters (OKX / Bybit / Bitget) + Auto-Routing

**Status:** ✅ Complete · **MOCK by default — no real orders** · feature-flagged · protected V10 engines untouched
**Date:** 2026-05-31

Generalises the Sprint 20F adapter layer from a single exchange (Binance) to a
unified, four-exchange live-trading surface: **OKX**, **Bybit**, and **Bitget**
join Binance on the same `ExchangeAdapter` interface, and `open` gains a
`Signal → Exchange Adapter → Execution` auto-routing path. The defining
property is unchanged: **no real order is ever placed unless the live-trading
gate is fully open** (`LIVE_TRADING_ENABLED=true` AND `MOCK_EXCHANGE_MODE=false`).

---

## What shipped

### New adapters `app/exchange_adapters/`
| File | Exchange / API | Signing |
|------|----------------|---------|
| `okx.py` | OKX USDT perpetual-swap (`/api/v5`) | `base64(HMAC-SHA256(secret, ts+METHOD+path+body))`, ISO-8601 ms timestamp, **passphrase** header. `BTCUSDT → BTC-USDT-SWAP`. |
| `bybit.py` | Bybit v5 linear USDT-perp | `hex(HMAC-SHA256(secret, ts+key+recvWindow+payload))`, payload = query (GET) or JSON body (POST). |
| `bitget.py` | Bitget v2 mix (`USDT-FUTURES`) | `base64(HMAC-SHA256(secret, ts+METHOD+path+body))`, ms-epoch timestamp, **passphrase** header. |

Each implements the full `base.py` contract — connect / balance / positions /
`open_order` / `close_order` / `set_tp_sl` / `set_leverage` / `set_margin_type`
/ `get_order_status` — returns the shared result dataclasses with `mode=LIVE`,
and carries its own `_guard()` (defense in depth) that refuses to touch the
network unless the gate is open.

### Factory `__init__.py`
- `_LIVE_ADAPTERS` extended to `{binance, okx, bybit, bitget}`; `resolve_adapter()`
  now dispatches to the right LIVE class (passphrase threaded to OKX/Bitget) —
  still only when `live_gate_open() AND creds present`, else `MockExchangeAdapter`.
- `PASSPHRASE_EXCHANGES = ("okx", "bitget")` exported for callers/UI.

### Auto-routing `app/live_trading/`
- `service.connected_exchanges()` — exchanges the user has a `CONNECTED` vault
  account for (20C).
- `service.route_exchange()` — picks the preferred exchange if connected, else
  the first connected; raises `400` when nothing is connected.
- `open_position()` accepts `exchange="auto"` (now the **schema default**) and
  resolves the concrete exchange via `route_exchange` before executing.
- `router.py` — new `GET /api/live/exchanges` returns `supported` /
  `connected` / `auto_routes_to` + gate status.

### TP/SL per venue
- OKX: `order-algo` OCO (`tpTriggerPx`/`slTriggerPx`).
- Bybit: `position/trading-stop` (`takeProfit`/`stopLoss`).
- Bitget: `place-tpsl-order` (`pos_profit`/`pos_loss` plans).

### The gate (unchanged, now four-wide)
1. `resolve_adapter()` returns a LIVE adapter for any of the four exchanges
   **only** when the gate is open and creds are present; otherwise MOCK.
2. Every adapter's `_guard()` re-checks before each network call.
3. Live API mounted only when `LIVE_TRADING_API_ENABLED=true`; mounting it does
   **not** enable real orders.
4. Each open still passes the 20E safety gate first.

---

## Validation
- Full suite: **152 passed** (139 prior + 13 new in `tests/test_exchange_adapters.py`).
  - **Signature-format tests** for OKX/Bybit/Bitget lock the exact prehash
    concatenation order (the #1 cause of silent auth failures), recomputed
    independently from each documented spec.
  - `to_inst_id` symbol mapping (`BTCUSDT → BTC-USDT-SWAP`).
  - **Routing matrix:** `resolve_adapter` returns the correct LIVE class per
    exchange when the gate is open; MOCK for all three when closed; passphrase
    threaded to OKX/Bitget.
  - **Guards:** every 20G adapter `_guard()` raises when the gate is closed,
    passes when open.
  - **Auto-routing:** prefers a connected match, falls back to first connected,
    ignores non-`CONNECTED` accounts, raises `400` when nothing connected.
- Real network calls intentionally **not** exercised in CI (no keys/network) —
  only signing, gating, and routing are unit-tested.

## Config
No new flags. Reuses `LIVE_TRADING_ENABLED` + `MOCK_EXCHANGE_MODE` (execution
gate) and `LIVE_TRADING_API_ENABLED` (API mount). Going live on any venue
requires a vaulted key for that exchange (with passphrase for OKX/Bitget),
`MOCK_EXCHANGE_MODE=false`, and `LIVE_TRADING_ENABLED=true`.

## Notes / follow-ups
- Future-ready: adding hyperliquid/mexc/gate/kucoin is now just a new adapter +
  one `resolve_adapter` branch.
- Next: Admin Dashboard.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer — no changes.
