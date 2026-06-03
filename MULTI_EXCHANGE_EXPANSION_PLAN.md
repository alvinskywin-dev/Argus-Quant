# Multi-exchange Live Expansion — Plan

**Date:** 2026-06-03
**Status:** 📋 Plan only — no code change, nothing enabled. Expansion proceeds
**after** the Binance pilot is stable, one exchange at a time.
**Order:** 1) Bybit → 2) OKX → 3) Bitget.

> Hard rule: no exchange is enabled for real orders by default. Each stays MOCK
> until its checklist below is green and it has passed its own testnet/sandbox
> validation, exactly like Binance (phase 21F). The single `live_gate_open()`
> chokepoint and the multi-user beta caps apply to every exchange.

---

## 1. Current state (what already exists)

`resolve_adapter()` already routes to `BinanceFuturesAdapter`, `OKXAdapter`,
`BybitAdapter`, `BitgetAdapter`, all MOCK-by-default with the per-adapter
`_guard()` re-checking the live gate. Permission validators exist for all four
(`validate_binance/okx/bybit/bitget`). Reconciliation, recovery, accounting, and
order-failure classification are exchange-agnostic (they operate through the
adapter interface + DB), so they extend to any adapter that fully implements
that interface.

### Adapter method coverage

| Capability | Binance | Bybit | OKX | Bitget |
|------------|:------:|:-----:|:---:|:------:|
| get_balance / get_positions | ✅ | ✅ | ✅ | ✅ |
| set_leverage / set_margin_type | ✅ | ✅ | ✅ | ✅ |
| open_order / close_order | ✅ | ✅ | ✅ | ✅ |
| set_tp_sl | ✅ | ✅ | ✅ | ✅ |
| get_order_status | ✅ | ✅ | ✅ | ✅ |
| **get_open_orders** | ✅ | ❌ | ❌ | ❌ |
| **cancel_all_orders** (reduce-only cleanup) | ✅ | ❌ | ❌ | ❌ |
| **symbol filters + precision** (`_symbol_filters`) | ✅ | ❌ | ❌ | ❌ |
| testnet/sandbox base URLs | ✅ | ❌ | ❌ | ❌ |
| read-only preflight + smoke script | ✅ | ❌ | ❌ | ❌ |

**Consequence:** OKX/Bybit/Bitget cannot yet support emergency close
(needs `cancel_all_orders`), precision-safe order sizing, or a testnet dry-run.
These are the gaps each expansion step must close.

## 2. Per-exchange checklist (apply to Bybit, then OKX, then Bitget)

For each exchange, in its own focused PR, gated and MOCK-by-default:

- [ ] **Sandbox/testnet** base + WS URLs and a `*_TESTNET_ENABLED` flag,
      mirroring `binance_testnet.py` (refuse mainnet URL in testnet mode).
- [ ] **Permission validator** verified live (trade enabled, withdrawal
      disabled) against the sandbox.
- [ ] **Symbol filters + precision** enforcement (min qty / step / tick / min
      notional) so orders round correctly — port the Binance approach.
- [ ] **open_order** (MARKET + LIMIT) with precision applied.
- [ ] **TP/SL** placement (`set_tp_sl`) verified, partial-failure → UNSAFE.
- [ ] **close_order** (reduce-only).
- [ ] **get_positions** / **get_open_orders**.
- [ ] **cancel_order** / **cancel_all_orders** (reduce-only cleanup) — required
      for emergency close.
- [ ] **emergency close** end-to-end (reconcile → cancel working orders →
      reduce-only close).
- [ ] **Reconciliation** read path verified (DB ↔ exchange drift).
- [ ] **Recovery** read path verified (rebuild open state on startup).
- [ ] **Accounting** (net-PnL) records on close.
- [ ] **Failure classification** maps the exchange's error codes into the
      order-failure engine.
- [ ] **Smoke script** `scripts/<exchange>_testnet_preflight.py` (read-only by
      default; real order only behind an explicit `--execute-test-order` flag),
      mirroring the Binance one.
- [ ] **Tests** for the gate, precision/rounding, validator mode, and the
      read-only flow (network paths exercised manually, never in CI).

## 3. Sequencing & exit criteria

1. **Bybit** first (closest API semantics to Binance USDT-perp).
2. **OKX** next (instId/contract-size nuances; passphrase auth).
3. **Bitget** last.

An exchange is "live-ready" only when: its checklist is green, its testnet
validation passed, a tiny gated pilot (same shape as the Binance 20–50 USDT
pilot) succeeded, and the multi-user beta caps have been exercised against it.
Roll out behind the existing flags — never flip `MOCK_EXCHANGE_MODE` globally
to onboard one exchange.

## 4. Risks

- **Per-exchange precision/filter rules differ** — incorrect rounding is the
  most common live-order rejection; cover with unit tests per exchange.
- **Reduce-only / position-mode semantics differ** (hedge vs one-way) — verify
  emergency close cannot ever open an opposite position.
- **Rate limits & error-code taxonomies differ** — extend failure
  classification before enabling, or retries may mis-fire.

## 5. Commit

`Add multi-exchange live expansion plan`

## 6. Guarantees

Plan only — no adapter enabled, no gate changed, no signal change, no secret,
not pushed.
