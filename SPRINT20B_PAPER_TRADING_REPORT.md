# Sprint 20B — Paper Trading System

**Status:** ✅ Complete · feature-flagged (`PAPER_TRADING_ENABLED`) · protected V10 engines untouched
**Date:** 2026-05-31

Every SaaS user (Sprint 20A) gets a virtual USDT-margined futures account
(default **10,000 USDT**) with positions, orders, PnL, leverage, margin,
funding, liquidation, and full trade history — all simulated, zero real risk.

---

## What shipped

### New package `app/paper_engine/`
| File | Responsibility |
|------|----------------|
| `math.py` | Pure futures maths: quantity, margin, liquidation price, unrealized PnL, ROE, funding, risk-based sizing. No DB — fully unit-tested and reusable by the 20D auto engine. |
| `service.py` | Per-account DB logic over the `paper_*` tables. Raises `PaperError(status, detail)`. |
| `schemas.py` | Pydantic request/response models. |
| `router.py` | `/api/paper/account/*` HTTP layer (auth-gated). |
| `__init__.py` | `setup_paper(app)` — idempotent router + error-handler mount. |

### Database (new per-user tables, created by `init_db()`)
- `paper_accounts` — one per `auth_users.id`: initial/current balance, currency, default leverage, `auto_follow`.
- `paper_account_positions` — open/closed positions: entry, qty, notional, leverage, **margin**, **liquidation_price**, SL/TP1-3, realized PnL, funding, status (OPEN/CLOSED/LIQUIDATED).
- `paper_orders` — MARKET (immediate fill) / LIMIT (rests NEW); reduce-only close orders recorded.
- `paper_trades` — realized close ledger (entry/exit, PnL, ROE%, funding, reason: TP1/2/3/SL/MANUAL/LIQUIDATION).

> **Naming note:** the legacy *global* paper engine already owns `paper_positions`
> (`app/paper/trading.py`, public dashboard). It is left fully intact; the new
> per-user position table is `paper_account_positions`.

### Accounting model (isolated margin)
- `balance` = realized wallet. Opening locks **margin** (`notional / leverage`); `available = balance − Σ open margin`. Opens are rejected when margin > available.
- `equity = balance + unrealized PnL`; mark prices come from the live `ws_engine.latest_prices` cache (falls back to entry).
- Close realizes `gross − funding` into balance; **liquidation** caps loss at the full margin.
- Liquidation price: LONG `entry·(1−1/L+mmr)`, SHORT `entry·(1+1/L−mmr)`, `mmr=0.5%`.

### API (mounted only when `PAPER_TRADING_ENABLED=true`; requires a 20A token)
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/paper/account/` | Dashboard summary: balance, equity, used/available margin, unrealized, **open positions, total PnL, daily PnL, win rate**. |
| POST | `/api/paper/account/open` | Manual open (margin or notional + leverage). |
| POST | `/api/paper/account/copy` | **Copy Signal** → open from a signal (risk-based sizing). |
| POST | `/api/paper/account/simulate` | **Simulate Trade** → dry-run PnL/ROE projection at TP1-3 & SL (no writes). |
| POST | `/api/paper/account/auto-follow` | **Auto Follow** toggle (flag stored; auto-execution wires in 20D). |
| POST | `/api/paper/account/positions/{id}/close` | Close at mark/given price. |
| POST | `/api/paper/account/reset` | Reset to starting balance, wipe positions/orders/trades. |
| GET | `/api/paper/account/positions` · `/orders` · `/trades` | Listings (positions enriched with live mark + uPnL + ROE). |

The three signal-card actions from the spec — **Copy Signal / Auto Follow /
Simulate Trade** — map to `/copy`, `/auto-follow`, `/simulate`.

---

## Validation
- `docker compose build` — clean.
- Full suite: **103 passed** (92 prior + 11 new `tests/test_paper_engine.py`).
- Route-mount check with both flags on: 10 paper routes registered; legacy `/api/paper/positions` confirmed intact.
- End-to-end vs Postgres (`tests/e2e_paper_manual.py`): demo account (10k) → simulate → manual open (liq=90.5 @10x) → insufficient-margin rejection → copy-signal → used-margin tracking → close +10% = **+500 USDT / 100% ROE** → balance/daily-PnL/win-rate update → orders+trades history → reset. Test data cleaned up.

## Config
Uses `PAPER_TRADING_ENABLED`, `DEFAULT_DEMO_BALANCE`, `PAPER_RISK_PER_TRADE_PCT`.
This commit also registers the V11 platform flags added to `.env` last turn
(`paper_trading_enabled`, `exchange_api_vault_enabled`, `mock_exchange_mode`,
`default_demo_balance`) as real `Settings` fields so they are honored, not ignored.

## Notes / follow-ups
- Paper endpoints require `AUTH_ENABLED=true` (per-user identity).
- `auto_follow` only stores intent in 20B; auto-execution on new signals is Sprint 20D.
- Limit-order matching and periodic funding/liquidation sweeps are stubbed for a later tracker pass; `check_liquidations()` exists and closes crossed positions on demand.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer · legacy global paper engine — no changes.
