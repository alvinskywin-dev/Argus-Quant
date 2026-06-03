# Sprint 22A — Portfolio Exposure + Position Lock Engine

## Goal
Stop the system from stacking many highly-correlated, same-direction bets
(BTC/ETH/SOL/DOGE all LONG ≈ one large correlated LONG) and from breaching
per-user open / loss limits.

## What shipped
- `app/risk/portfolio_exposure.py` — pure, self-contained engine.
- API: `GET /api/portfolio/exposure` (read-only diagnostics).
- Config block in `app/config.py` + `.env.example`.
- 30 unit tests in `tests/test_portfolio_exposure.py`.

## Engine surface
- `PortfolioExposureState` — open positions, pending orders, correlated groups,
  long/short counts, exposure score, daily PnL, locked symbols.
- `can_open_position()` — the gate. Returns an `ExposureDecision`.
- `calculate_exposure_score()` — 0-100 (directional imbalance 45% + correlation
  clustering 35% + concentration 20%).
- `is_correlated_symbol()`, `is_symbol_locked()`, `has_pending_order()`.

## Rejection rules (in order)
A new entry is rejected when:
1. the symbol already has an open position **or** is locked (`SYMBOL_LOCK_ENABLED`),
2. the symbol has a pending order (`PENDING_ORDER_LOCK_ENABLED`),
3. the user is at `MAX_OPEN_POSITIONS_PER_USER`,
4. same-direction open positions ≥ `MAX_SAME_DIRECTION_POSITIONS`,
5. correlated same-direction positions in a group ≥ `MAX_CORRELATED_POSITIONS`,
6. daily loss ≥ `MAX_DAILY_LOSS_PERCENT`.

`CORRELATION_GROUPS` format: `LEADER:SYM,SYM;GROUP:SYM,SYM`. The leader (e.g.
`BTC`) is itself a member of its group. Symbols are normalised (`BTCUSDT`→`BTC`).

## Diagnostics
`exposure_score`, `same_direction_count`, `correlated_group`,
`daily_loss_percent`, `portfolio_reject_reason`.

## Safety / compatibility
- Feature-flagged: `PORTFOLIO_EXPOSURE_ENGINE_ENABLED=false` → always allows
  (identical prior behaviour), but the exposure score is still computed so the
  API/UI can show the picture in shadow.
- Can only **reject**, never force, an entry. Places no orders, touches no DB
  schema, imports only `settings`.

## Validation
`compileall` ✓ · `ruff` ✓ · `black` ✓ · `pytest tests/test_portfolio_exposure.py`
→ 30 passed.
