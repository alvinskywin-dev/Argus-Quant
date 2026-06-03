# Phase 21F — Binance Testnet Validation — Report

**Date:** 2026-06-03
**Status:** ✅ Implemented, tested, image builds. ⏸️ Live testnet run **STOPPED**
pending Binance **testnet** API keys (rule 10).
**Tests:** 349 passing (6 new in `tests/test_binance_testnet.py`).
**Flag:** `BINANCE_TESTNET_ENABLED=false` by default — the script refuses to run.

---

## 1. What shipped

### Config (`.env.example`, `app/config.py`)
Dedicated, separate-from-production testnet settings:
```
BINANCE_TESTNET_ENABLED=false
BINANCE_TESTNET_BASE_URL=https://testnet.binancefuture.com
BINANCE_TESTNET_WS_URL=wss://stream.binancefuture.com/ws
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_API_SECRET=
```

### Guard layer — `app/exchange_vault/binance_testnet.py`
Pure, unit-testable resolver `resolve_testnet_config()` that **refuses**
(`BinanceTestnetGuardError`) unless, in order: the feature flag is on → the base
URL is the testnet host → both keys are present. `is_testnet_url()` accepts only
`testnet.binancefuture.com` and never `fapi.binance.com`, so a mainnet URL can
never be resolved. Returns `mode="TESTNET"`.

### Smoke script — `scripts/binance_testnet_preflight.py`
Read-only by default; runs the full manual flow and prints each step:

| Step | Check | Source |
|------|-------|--------|
| 1 | validate key (signed read) | `permission_validator.validate_binance` |
| 2 | account check (balance) | `run_binance_preflight` |
| 3 | exchange filters (minQty/stepSize/tickSize/minNotional) | `run_binance_preflight` + `parse_symbol_filters` |
| 4 | tiny test-position planning | `plan_order_quantity` |
| 5 | TP/SL capability (tick/step present) | symbol filters |
| 6 | reconciliation read-only (positionRisk readable) | signed GET |
| 7 | recovery read-only (openOrders readable) | signed GET |

**No order is placed** unless `--execute-test-order` AND
`--confirm "I UNDERSTAND THIS PLACES A REAL TESTNET ORDER"` AND the read-only
checks passed. The execute path posts directly to the resolved **testnet** base
URL (which the guard proves can only be testnet), so it never touches the live
gate and **mainnet remains fully gated**.

### Existing pieces reused (no duplication)
`fapi_base`, `classify_clock_skew` (clock-drift vs recvWindow), `SymbolFilters` /
`parse_symbol_filters`, `round_step_*`, `check_min_notional`,
`plan_order_quantity`, `run_binance_preflight`, and the testnet-capable
`BinanceFuturesAdapter` were already present from Sprint 21F and are wired in.

## 2. Tests (`tests/test_binance_testnet.py`, 6) — all required cases

- testnet **disabled** refuses the script
- **missing keys** refuses the script (key or secret)
- **non-testnet URL** refuses the script (mainnet base URL rejected)
- adapter **picks the testnet URL** (`_TESTNET_URL` vs `_PROD_URL`)
- validator returns **TESTNET mode**
- **no mainnet URL** accepted in testnet mode (`is_testnet_url` matrix)

## 3. Validation performed

| Step | Result |
|------|--------|
| `python -m compileall app tests scripts` | ✅ clean |
| `pytest -q tests/test_binance_testnet.py` | ✅ 6 passed |
| `pytest -q` (full) | ✅ 349 passed |
| `ruff check` / `black --check` | ✅ clean |
| `docker compose build bot` | ✅ image built |
| Live testnet round-trip | ⏸️ **STOPPED** — needs testnet keys |

## 4. STOP — exact setup to finish live testnet validation

1. Create a **Binance Futures Testnet** account at
   <https://testnet.binancefuture.com> (independent from production).
2. Generate an **API key + secret** there; enable **Futures trading** permission;
   **withdrawal is not applicable** on testnet.
3. Put them in `.env` (never commit):
   ```
   BINANCE_TESTNET_ENABLED=true
   BINANCE_TESTNET_API_KEY=<testnet key>
   BINANCE_TESTNET_API_SECRET=<testnet secret>
   ```
4. Read-only validation (places no orders):
   ```
   docker compose run --rm bot python -m scripts.binance_testnet_preflight --symbol BTCUSDT --notional 20
   ```
   Expect: steps 1–7 PASS, `READ-ONLY RESULT: PASS ✅`.
5. (Optional) one tiny real **testnet** order, then auto reduce-only close:
   ```
   docker compose run --rm bot python -m scripts.binance_testnet_preflight \
     --symbol BTCUSDT --notional 20 --execute-test-order \
     --confirm "I UNDERSTAND THIS PLACES A REAL TESTNET ORDER"
   ```

## 5. Commit

`Validate Binance testnet execution readiness`

## 6. Guarantees preserved

TESTNET only · no mainnet order possible from this path · no real money · live
gate untouched · mock/paper intact · no signal/scanner change · all routes
preserved · no secret committed · not pushed.

Next roadmap phase: **20–50 USDT Live Pilot** (gated code; will NOT run
automatically and STOPS before any real order — needs a production Binance key).
