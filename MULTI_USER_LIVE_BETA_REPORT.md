# Multi-user Live Beta — Report

**Date:** 2026-06-03
**Status:** ✅ Gated foundation implemented, tested, image builds.
**Tests:** 385 passing (15 new in `tests/test_live_beta.py`).
**Flag:** `LIVE_BETA_ENABLED=false` by default — API unmounted, gate is a no-op.

---

## 1. What shipped

A controlled-access layer so a small allowlist of users can live-trade under
hard per-user and platform-wide exposure limits — disabled by default.

- **Model** `app/live_beta/models.py` → `live_beta_members`: one row per user
  (status PENDING/APPROVED/REJECTED/SUSPENDED, per-user `max_notional` /
  `max_positions` / `allowed_exchanges`, `invite_code_used`,
  `risk_agreement_accepted_at`, `approved_by/at`). Registered in `init_db`.
- **Service** `app/live_beta/service.py`:
  - `request_access` — requires the beta open, the risk agreement accepted, a
    valid invite code (if configured), and capacity (`LIVE_BETA_MAX_USERS`);
    creates PENDING, or APPROVED when admin approval is not required.
  - `approve` / `reject` / `suspend` — admin transitions, audit-logged.
  - **`beta_gate(db, user_id, exchange, symbol, notional)`** — the reusable
    pre-trade check enforcing: approved membership + accepted risk agreement,
    the exchange allowlist, per-user position cap, per-user capital limit,
    per-symbol exposure limit, and the **global** beta exposure cap.
- **API** `app/live_beta/router.py` (`/api/live-beta/*`, mounted only when the
  flag is on): `POST /request`, `GET /status`, and admin
  `GET /admin/members`, `POST /admin/{approve,reject,suspend}`
  (`require_role("ADMIN")`).
- **Integration:** `live_trading.service.open_position` calls `beta_gate`
  immediately after the safety layer — but only when `LIVE_BETA_ENABLED`, so
  the default execution path is byte-for-byte unchanged. A blocked order is
  audited and rejected with the reason.
- **Config + `.env.example`:** `LIVE_BETA_ENABLED`, `LIVE_BETA_MAX_USERS`,
  `LIVE_BETA_REQUIRE_ADMIN_APPROVAL`, `LIVE_BETA_INVITE_CODE`,
  `LIVE_BETA_GLOBAL_MAX_NOTIONAL`, `LIVE_BETA_DEFAULT_USER_MAX_NOTIONAL`,
  `LIVE_BETA_DEFAULT_MAX_POSITIONS`, `LIVE_BETA_PER_SYMBOL_MAX_NOTIONAL`,
  `LIVE_BETA_ALLOWED_EXCHANGES`.

## 2. Roadmap requirement coverage

| Requirement | Where |
|-------------|-------|
| Live user allowlist | `live_beta_members` + approval flow |
| Per-user capital / position limits | `max_notional` / `max_positions` in `beta_gate` |
| Per-user exchange allowlist | `allowed_exchanges` in `beta_gate` |
| Per-symbol exposure limit | `LIVE_BETA_PER_SYMBOL_MAX_NOTIONAL` |
| Global exposure cap | `LIVE_BETA_GLOBAL_MAX_NOTIONAL` |
| Beta invite code | `LIVE_BETA_INVITE_CODE` |
| Admin approval required | `LIVE_BETA_REQUIRE_ADMIN_APPROVAL` + `/admin/approve` |
| Risk agreement accepted_at | `risk_agreement_accepted_at` |
| Audit every live action | admin transitions logged; open path audits beta rejects |
| Admin dashboard (live users etc.) | `GET /api/live-beta/admin/members` + existing `/api/admin/*` (overview, safety, kill switch) |

## 3. Tests (`tests/test_live_beta.py`, 15)

Request flow (refused when disabled / no risk acceptance / bad invite / full;
PENDING when approval required; auto-APPROVED otherwise) and `beta_gate`
(no-op when disabled; blocks non-member, unapproved, disallowed exchange,
position cap, per-user notional, per-symbol, global cap; allows within limits).

## 4. Validation

| Step | Result |
|------|--------|
| `python -m compileall app tests` | ✅ clean |
| `pytest -q tests/test_live_beta.py` | ✅ 15 passed |
| `pytest -q` (full) | ✅ 385 passed |
| `ruff check` / `black --check` | ✅ clean |
| 6 beta routes mount (flag on) | ✅ verified + added to route inventory lock |
| `docker compose build bot` | ✅ image built |

## 5. Commit

`Add controlled multi-user live beta foundation`

## 6. Guarantees preserved

Beta disabled by default (API unmounted, gate no-op) · real orders still require
the live execution gate · default `open_position` path unchanged · mock/paper
intact · no signal/scanner change · all prior routes preserved · no secret
committed · not pushed.

Next roadmap phase: **Multi-exchange Expansion** (plan document only — Bybit →
OKX → Bitget readiness checklist; no code enablement by default).
