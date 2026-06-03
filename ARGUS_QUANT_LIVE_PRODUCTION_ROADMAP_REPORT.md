# ARGUS QUANT — Live Production Roadmap — Final Review

**Date:** 2026-06-03
**Branch:** `chore/devops-quality-foundation`
**Scope:** the post-hardening live-production roadmap (P11 → multi-exchange plan).
**Nothing pushed; nothing auto-enabled.** Every live capability is flag-gated OFF
by default.

---

## 1. Completed phases

| # | Phase | Outcome | Live validation |
|---|-------|---------|-----------------|
| 1 | **P11 Google OAuth** | Login flow, flag-gated; links/creates users; no token stored/logged | ⏸️ needs Google creds |
| 2 | **21F Binance Testnet** | Dedicated testnet config + guard (never mainnet) + 7-step smoke script | ⏸️ needs testnet keys |
| 3 | **20–50 USDT Live Pilot** | Fully-gated; manual confirmation + all safety checks; delegates to audited open | 🚫 no real order placed |
| 4 | **Router Decomposition** | 142-path route surface locked by regression test | ✅ |
| 5 | **Prometheus + Grafana** | Expanded `/metrics` + opt-in stack (5 dashboards) | ✅ configs validated |
| 6 | **Cloudflare + Nginx SSL** | Reverse-proxy config (nginx -t valid) + deployment guide | ✅ syntax valid |
| 7 | **Multi-user Live Beta** | Gated membership + per-user/symbol/global exposure gate | ✅ (MOCK by default) |
| 8 | **Multi-exchange Expansion** | Readiness plan (Bybit→OKX→Bitget); no enablement | ✅ plan only |

## 2. Commits (this roadmap)

```
603c5a5 style: black-format legacy scripts/rebuild_performance.py
321635d Add multi-exchange live expansion plan
c46ffdc Add controlled multi-user live beta foundation
0d32a24 Add Cloudflare and Nginx SSL deployment guide
39f8ac5 Add production Prometheus and Grafana monitoring
14a48fc Refactor dashboard routes into modular routers
0800a2c Add gated Binance live pilot mode
189498f Validate Binance testnet execution readiness
486394a P11 add Google OAuth login
```
(Preceded by the 12-phase hardening program; see `ARGUS_QUANT_PRODUCTION_REVIEW.md`.)

## 3. Test & build results

| Check | Result |
|-------|--------|
| `python -m compileall app tests scripts` | ✅ clean |
| `pytest -q` | ✅ **385 passed** |
| `ruff check .` | ✅ clean |
| `black --check .` | ✅ clean (191 files) |
| `mypy app` | ⚠️ advisory — 53 findings (non-blocking by project policy; 17 are the known Starlette `add_exception_handler` typing quirk shared by every `setup_*`) |
| `docker compose build` | ✅ bot image built |
| `docker compose ps` | ✅ bot + postgres + redis healthy (running) |
| `docker logs signals-bot --since=5m \| grep -Ei error…` | ✅ no errors in window |

## 4. Live-safety status

- The single `live_gate_open()` chokepoint still governs every real order; all
  adapters MOCK by default; per-adapter `_guard()` re-checks the gate (hardening
  Phase 7). The pilot and beta add **further** gates on top — they can only
  ever restrict, never bypass.
- **Operational flag:** the **deployed instance on this host currently has the
  live gate OPEN** (`/api/live/status` → `mode: LIVE`). This comes from the
  host's git-ignored `.env`, not from any committed default (code + `.env.example`
  ship gate-closed). **Action before public launch: confirm production keeps
  the gate closed until validated**, or that only the intended pilot key is
  connected.
- `AUTO_TRADING_ENABLED` remains hard-locked false; no autonomous execution.

## 5. Go / No-Go

| Target | Status | Rationale |
|--------|:------:|-----------|
| **a) Paper public beta** | 🟢 **GO** | Paper engine + safety layer + dashboard shipped and tested; no real funds. Keep `LIVE_TRADING_ENABLED=false`. |
| **b) 20–50 USDT Binance pilot** | 🟡 **CONDITIONAL** | Code is gated and tested, but **requires**: testnet validation run with real keys (21F), one trade-only/withdrawal-disabled Binance key, operator sets `LIVE_PILOT_*` + opens the gate, manual confirmation per order. Not before those steps. |
| **c) Multi-user live beta** | 🔴 **NO-GO (yet)** | Foundation shipped + tested, but gated OFF. Prerequisite: the single-user pilot (b) is stable first; then enable `LIVE_BETA_ENABLED` with admin approval + caps. |
| **d) Multi-exchange live** | 🔴 **NO-GO** | Only Binance is execution-complete. OKX/Bybit/Bitget lack `get_open_orders`/`cancel_all_orders`/precision/testnet (see `MULTI_EXCHANGE_EXPANSION_PLAN.md`). Bybit→OKX→Bitget, each after its own testnet + pilot. |

## 6. What remains before public launch

1. Provide **Google OAuth** creds → validate P11 live (`GOOGLE_OAUTH_REPORT.md §5`).
2. Run **Binance testnet** validation with real testnet keys (`BINANCE_TESTNET_VALIDATION_REPORT.md §4`).
3. Execute the **gated pilot** once, manually, with a tiny capped key.
4. Stand up **TLS** (Cloudflare + Nginx) and the **monitoring** overlay in prod.
5. Confirm the **production `.env`** trading-gate posture is intentional.
6. Only then consider **multi-user beta**; multi-exchange follows per its plan.

## 7. Rollback plan

- **Disable any live capability instantly** (no deploy): set the relevant flag
  false in `.env` and restart — `LIVE_TRADING_ENABLED=false` (closes the gate →
  everything MOCK), and/or `LIVE_PILOT_ENABLED` / `LIVE_BETA_ENABLED` false
  (routes 404, gates inert).
- **Emergency stop while live:** admin global kill switch
  (`/api/admin/safety/kill-all`) blocks all opens; per-position reduce-only
  **emergency close** (`/api/live/.../emergency-close` or the pilot equivalent).
- **Code rollback:** every phase is one clean, revertible commit; `git revert
  <sha>` removes a phase without touching others. The branch has not been
  pushed, so reverting locally is sufficient.
- **Data:** all new schema is additive (idempotent `ALTER … IF NOT EXISTS` /
  new tables); rolling back code leaves existing rows intact.

---

**Bottom line:** the platform is **GO for paper public beta now**, and
**ready-but-gated** for a carefully-operated Binance pilot once the credential
and testnet steps are done. Multi-user and multi-exchange live remain
deliberately closed until their prerequisites are met. No real order has been
placed by this work, and nothing has been pushed.
