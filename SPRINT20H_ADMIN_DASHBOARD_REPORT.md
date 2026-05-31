# Sprint 20H — Admin Dashboard

**Status:** ✅ Complete · **ADMIN-only · read-oriented · no secrets exposed** · feature-flagged · protected V10 engines untouched
**Date:** 2026-05-31

The final 20-series deliverable: a platform-oversight API for operators. Every
route requires the `ADMIN` role; it aggregates across the multi-user SaaS tables
(20A–20F) for a one-call overview, paginated user list, per-user detail, and a
live audit feed, plus suspend/activate moderation. It **never** returns decrypted
exchange credentials and **reuses** the 20E global emergency stop rather than
duplicating it.

---

## What shipped

### `app/admin/` package
| File | Responsibility |
|------|----------------|
| `service.py` | DB aggregation (overview rollup, user list w/ grouped per-user counts — no N+1, user detail, audit feed) + `set_user_status` moderation. `AdminError(status_code, detail)`. |
| `router.py` | `/api/admin/*`, **every** endpoint gated by `require_role("ADMIN")`. |
| `schemas.py` | `SetUserStatusIn` (status ∈ ACTIVE/SUSPENDED). |
| `__init__.py` | `setup_admin(app)` — idempotent mount + error handlers, mirroring 20E/20F. |

### Endpoints (all ADMIN-only)
| Method · Path | Returns |
|---|---|
| `GET /api/admin/overview` | Users by role/status, exchange accounts by exchange + connected count, open positions (LIVE/MOCK split), auto-trading-enabled users, active kill switches, realized PnL, **global_kill**, **live_gate_open**. |
| `GET /api/admin/users` | Paginated list (`limit`/`offset`/`status`/`role`); each row carries connected-exchange count, auto-trading flag, kill-switch flag. |
| `GET /api/admin/users/{id}` | Profile + exchange accounts (**last4 + status only**) + auto config + safety state + open positions. |
| `GET /api/admin/audit` | Recent `live_audit_log` feed (optional `user_id` filter). |
| `PUT /api/admin/users/{id}/status` | Suspend / activate a user (a suspended user can no longer log in). |

### Mounting / flag
- Mounted in `create_app()` only when `ADMIN_DASHBOARD_ENABLED=true` (new, default **off**); `.env.example` documented. Requires `AUTH_ENABLED` and an ADMIN-role user (the first registered account becomes ADMIN).

### Safety properties
1. **RBAC:** `require_role("ADMIN")` on every route — a FREE/PREMIUM token gets `403`.
2. **No credential exposure:** exchange accounts surface `exchange/label/status/api_key_last4/can_*` only — never ciphertext or decrypted secrets.
3. **No new kill path:** global emergency stop is read from the 20E safety service, not re-implemented.
4. **Self-protection:** an admin cannot suspend their own account (`400`).
5. **New module, V10 untouched** — built as a separate package, feature-flagged.

---

## Validation
- Unit suite: **155 passed** (152 prior + 3 new `tests/test_admin.py`: the
  `set_user_status` guards — invalid status & self-suspend — and `AdminError`).
- Manual e2e vs Postgres (`tests/e2e_admin_manual.py`): FREE user → `403` on
  admin routes; overview rollup (`users.total≥2`, `live_gate_open=False`); user
  list shows `connected_exchanges=1`; **detail exposes only last4 — asserts no
  `api_secret`/`encrypted` anywhere in the payload**; audit feed reachable;
  suspend → login `403` → re-activate; admin self-suspend → `400`. Test data
  pre/post-purged (shared dev DB).

## Config (`.env` / `.env.example`)
`ADMIN_DASHBOARD_ENABLED=false` (mount the API; default off). No other new flags.

## Notes
- Sprint 20 SaaS series (20A–20H) is now feature-complete: auth, per-user paper
  trading, encrypted API vault, demo auto-engine, safety layer, Binance live,
  multi-exchange adapters + auto-routing, and this admin dashboard — all
  feature-flagged and MOCK/demo-safe by default.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer — no changes.
