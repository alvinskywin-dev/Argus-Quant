# Sprint 20E — Real Trading Safety Layer

**Status:** ✅ Complete · feature-flagged (`SAFETY_LAYER_ENABLED`, on by default) · protected V10 engines untouched
**Date:** 2026-05-31

Prevents account destruction by wrapping every auto open (Sprint 20D) with
protective checks, plus per-user and admin kill switches.

---

## What shipped

### New package `app/safety/`
| File | Responsibility |
|------|----------------|
| `rules.py` | Pure decision maths: correlation clustering, consecutive-loss count, loss-limit test, correlated-position count. Fully unit-tested. |
| `service.py` | The `check()` orchestrator (DB aggregation + lockout state), kill switches, status. Raises `SafetyError`. |
| `schemas.py` / `router.py` | `/api/safety/*` (user) and `/api/admin/safety/*` (ADMIN) APIs. |
| `__init__.py` | `setup_safety(app)`. |

### Database
- `safety_configs` — per-user limits: max daily/weekly loss %, max open, max correlated, max leverage, trade cooldown, loss-streak limit + cooldown hours.
- `safety_states` — mutable per-user state: kill switch, timed `disabled_until` + reason.
- Global emergency stop persisted in `system_settings` (`trading_global_kill`).

### The check (runs FIRST inside the auto engine, before sizing)
Order, most-severe first — any failure logs a `SKIP safety:<code>` execution:
1. **Global emergency stop** (admin) → blocks everyone.
2. **User kill switch** → blocks the user.
3. **Active lockout** (`disabled_until` in the future).
4. **Max daily loss** → locks until next UTC midnight.
5. **Max weekly loss** → locks until next UTC midnight.
6. **Loss-streak** (e.g. 3 in a row) → locks for the configured cooldown (default 24h).
7. **Trade cooldown** (transient).
8. **Max open positions.**
9. **Max correlated positions** (same correlation cluster + same side).

Spec examples both implemented: *3 losses in a row → disable 24h*; *5% daily loss → disable*. Loss limits and streak set a real `disabled_until`, so subsequent signals short-circuit until the lockout expires or the user resumes.

### API
- User: `GET/PUT /api/safety/config`, `GET /api/safety/status` (trading_enabled, daily/weekly PnL, loss streak, lockout reason), `POST /api/safety/kill`, `POST /api/safety/resume`.
- Admin (ADMIN role): `POST /api/admin/safety/kill-all` (instant global stop), `POST /api/admin/safety/resume-all`, `GET /api/admin/safety/state`.

### Integration
One guarded block added to the auto engine's `_maybe_open` (Sprint 20D code, my own module — no protected engine touched): the safety check runs before the 20D risk evaluation; on denial it logs a SKIP and returns. Gated by `SAFETY_LAYER_ENABLED`.

---

## Validation
- `docker compose build` — clean.
- Full suite: **129 passed** (125 prior + 4 new `tests/test_safety.py`: clustering, consecutive losses, loss-limit, correlated count).
- End-to-end vs Postgres (`tests/e2e_safety_manual.py`, two users):
  - **Correlated cap** — BTC (MAJOR) opens, ETH (MAJOR, same side) **blocked**, SOL (L1) opens.
  - **Loss-streak** — 2 SL losses (−$199) → 3rd signal **blocked**, status locked with reason "2 losses in a row".
  - **Resume** clears the lockout.
  - **User kill switch** blocks; **admin global kill-all** halts everyone and a FREE user is correctly **403'd**; resume-all restores.

## Debugging notes (issues found & fixed during this sprint)
All three were **test-harness** bugs, not engine bugs — the safety logic was correct throughout:
1. The manual e2e asserted on `on_new_signal`'s return value, which is a **multi-user** count; switched to asserting each user's own open positions.
2. A test PUT used `loss_streak_limit=99`, exceeding the schema cap (`le=50`), so the whole config update **422'd silently** and left `max_correlated` at its default — now the e2e asserts PUT success.
3. The e2e's cleanup queried emails case-sensitively, but auth **lowercases** stored emails, so leftover users were never purged and accumulated across runs. Fixed; the e2e now pre- and post-purges idempotently and seeds test signals as `CLOSED` to avoid the active-signal unique index.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer — no changes.
