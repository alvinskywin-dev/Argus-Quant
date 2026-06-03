# Live-Safety Audit V2

**Date:** 2026-06-03
**Scope:** The full path by which a signal could become a *real* exchange order
— the live-trading gate, the adapter factory, every LIVE adapter, and the
order-execution service.
**Verdict:** ✅ Architecture is sound. No path can place a real order with the
gate closed. One defense-in-depth consistency gap was found and fixed.

---

## 1. The gate

A real order requires **both** flags, checked by a single canonical function:

```python
# app/exchange_adapters/__init__.py
def live_gate_open() -> bool:
    return bool(settings.live_trading_enabled and not settings.mock_exchange_mode)
```

- Code defaults (`app/config.py`): `live_trading_enabled = False`,
  `mock_exchange_mode = True` → **gate closed by default**. ✅
- `.env.example` ships the same safe-closed values, with `LIVE_TRADING_API_ENABLED=false`. ✅

## 2. Chokepoint: `resolve_adapter()`

The factory is the only place that constructs a LIVE adapter. It returns a real
adapter **only** when `live_gate_open()` AND the exchange is supported AND
credentials are present; every other case returns `MockExchangeAdapter`. Covered
by the existing matrix in `tests/test_exchange_adapters.py`
(`test_resolve_*`). ✅

## 3. Defense-in-depth: per-adapter `_guard()`

Each LIVE adapter (binance/okx/bybit/bitget) calls `_guard()` at its network
chokepoint (`_request`), so even an adapter constructed directly cannot reach
the exchange with the gate closed.

**Finding V2-1 (fixed):** all four guards re-implemented the gate condition
inline —
`if not settings.live_trading_enabled or settings.mock_exchange_mode:` — four
independent copies of the gate logic. If the canonical gate definition ever
changed (e.g. a third condition added), the adapters would silently diverge and
could permit a real order the factory would have refused.

**Fix:** every guard now delegates to the single source of truth:

```python
@staticmethod
def _guard() -> None:
    if not live_gate_open():
        raise AdapterError("Live-trading gate is closed; refusing real order.")
```

Regression coverage added in `tests/test_exchange_adapters.py`:
- `test_all_adapter_guards_refuse_when_gate_closed` — all 4 adapters × 3 closed
  flag-states.
- `test_all_adapter_guards_pass_only_when_gate_open` — all 4 pass only when fully open.
- `test_guards_track_canonical_gate_definition` — forcing `live_gate_open()`
  closed makes every guard refuse even while the raw flags read "open", proving
  delegation rather than a divergent copy.

## 4. Execution service

`app/live_trading/service.py` applies, in order: the safety layer
(`safety.trading_blocked` — global/user kill switch + lockout), vault credential
resolution, then the adapter. Entry is persisted before TP/SL so a real fill is
never lost; a TP/SL failure marks the position `UNSAFE` (recovery/emergency-close
eligible) rather than silently dropping protection. `emergency_close_position`
is reduce-only and reconciles against the live exchange position first. ✅

## 5. Environment note (not a code defect)

The local dev `.env` on this machine has the gate **open**
(`LIVE_TRADING_ENABLED=true`, `MOCK_EXCHANGE_MODE=false`). `.env` is
git-ignored (not committed), so this is a per-host dev/testnet choice, not a
shipped default. **Production action:** confirm the deployed `.env` keeps the
gate closed until a host has been validated end-to-end via the read-only
Binance preflight (`binance_preflight`).

---

## Summary

| Layer | Status |
|-------|--------|
| Gate definition (single function) | ✅ |
| Safe code + `.env.example` defaults | ✅ |
| Factory chokepoint | ✅ (tested) |
| Per-adapter guard | ✅ fixed to delegate to canonical gate (tested) |
| Execution service ordering / safety layer | ✅ |
| Deployed `.env` hygiene | ⚠️ operational check (gate open on this dev host) |
