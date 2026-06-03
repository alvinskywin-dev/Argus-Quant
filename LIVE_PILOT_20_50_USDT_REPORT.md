# Live Pilot (20–50 USDT) — Report

**Date:** 2026-06-03
**Status:** ✅ Gated scaffolding implemented, tested, image builds.
⏸️ **No real order has been or will be placed by this work.** Live execution
**STOPPED** pending an explicit operator decision + a production Binance key.
**Tests:** 362 passing (13 new in `tests/test_live_pilot.py`).
**Flag:** `LIVE_PILOT_ENABLED=false` by default — every pilot route returns 404.

---

## 1. Design — defence in depth

A pilot order is impossible unless **all** of these hold simultaneously:

1. `LIVE_PILOT_ENABLED=true`
2. caller is the single `LIVE_PILOT_USER_ID`
3. exact confirmation phrase **"I UNDERSTAND THIS PLACES A REAL ORDER"**
4. request within hard limits: Binance-only, symbol ∈ {BTCUSDT, ETHUSDT},
   `leverage ≤ LIVE_PILOT_MAX_LEVERAGE` (default 3), `notional ≤
   LIVE_PILOT_MAX_NOTIONAL` (default 50), and **both** stop-loss and take-profit
   attached
5. `AUTO_TRADING_ENABLED` is **false**
6. safety layer clear (global kill / user kill / timed lockout)
7. position cap not exceeded and no existing open position on the symbol
8. and finally the **live execution gate** (`LIVE_TRADING_ENABLED=true AND
   MOCK_EXCHANGE_MODE=false`) — otherwise the underlying open runs **MOCK** and
   places nothing.

The order itself is delegated to the existing audited
`live_trading.service.open_position`, so the pilot adds gating but reuses the
proven (gate-checked, TP/SL-aware, failure-recording) execution path.

## 2. What shipped

- **Config** (`app/config.py`, `.env.example`): `LIVE_PILOT_ENABLED`,
  `LIVE_PILOT_USER_ID`, `LIVE_PILOT_MAX_NOTIONAL`, `LIVE_PILOT_MAX_POSITIONS`,
  `LIVE_PILOT_MAX_LEVERAGE`, `LIVE_PILOT_ALLOWED_SYMBOLS`,
  `LIVE_PILOT_REQUIRE_CONFIRMATION`.
- **Service** (`app/live_trading/pilot.py`):
  - `validate_pilot_request()` — pure static-limit checks (flag, auto-off,
    symbol, leverage, notional).
  - `pilot_preflight()` — full structured safety report (the 8 checks above,
    plus a best-effort balance read that is advisory in MOCK).
  - `pilot_open()` — manual-confirmation entry; refuses unless the preflight
    fully passes, then delegates to `service.open_position`.
  - `pilot_emergency_close()` — reduce-only close via the audited emergency path.
- **Routes** (on the live router, mounted with `LIVE_TRADING_API_ENABLED`;
  each returns 404 while the pilot flag is off):
  - `GET  /api/live/pilot/config`
  - `POST /api/live/pilot/preflight`
  - `POST /api/live/pilot/open`
  - `POST /api/live/pilot/emergency-close`
- **Schemas**: `PilotPreflightIn`, `PilotOpenIn` (requires stop_loss +
  take_profit + confirm), `PilotEmergencyCloseIn`.

## 3. Tests (`tests/test_live_pilot.py`, 13)

Static limits (disabled flag, symbol not allowed, leverage cap, notional cap,
auto-trading must be off); `pilot_open` guards (refused when disabled, wrong
confirmation, missing stop/TP); preflight (all-clear passes, blocked by safety
fails, wrong user fails, position-cap + duplicate-symbol fail); and that a
passing preflight delegates to `open_position`.

## 4. Validation performed

| Step | Result |
|------|--------|
| `python -m compileall app tests` | ✅ clean |
| `pytest -q tests/test_live_pilot.py` | ✅ 13 passed |
| `pytest -q` (full) | ✅ 362 passed |
| `ruff check` / `black --check` | ✅ clean |
| pilot routes mount (config/preflight/open/emergency-close) | ✅ verified |
| `docker compose build bot` | ✅ image built |
| **Any real order** | 🚫 **NOT executed — by design** |

## 5. Prerequisites before a real pilot (operator checklist)

Per the roadmap, this phase must NOT run automatically. Before enabling:

- Binance **testnet validation passed** (phase 21F) on this host.
- **One** API key, **trade permission ON**, **withdrawal DISABLED**, IP
  allow-list recommended.
- Binance only · one user · capital **20–50 USDT** · leverage **2–3x** · max
  **1–2** positions · symbols **BTCUSDT/ETHUSDT**.
- `AUTO_TRADING_ENABLED=false` stays false.

Then, to run the pilot live (operator, manually):
```
LIVE_PILOT_ENABLED=true
LIVE_PILOT_USER_ID=<the pilot user id>
AUTH_ENABLED=true
LIVE_TRADING_API_ENABLED=true
# the live execution gate (only when you are ready for a REAL order):
LIVE_TRADING_ENABLED=true
MOCK_EXCHANGE_MODE=false
```
1. `POST /api/live/pilot/preflight` → confirm every check `ok=true`.
2. `POST /api/live/pilot/open` with the confirmation phrase to place ONE order.
3. `POST /api/live/pilot/emergency-close` (reduce-only) if anything looks wrong.

## 6. Commit

`Add gated Binance live pilot mode`

## 7. Guarantees preserved

No order executed · pilot disabled by default (routes 404) · live gate still
required for any real fill · mock/paper intact · no signal/scanner change · all
prior routes preserved · no secret committed · not pushed.

Next roadmap phase: **Router Decomposition** (no external credentials required —
fully completable).
