# ALPHA RADAR SIGNALS V11 — Full Source Audit Report

**Date:** 2026-05-31 · **Branch:** `develop` · **Audited at commit:** `702101f` (Sprint 20H)
**Safety tag created:** `pre-v11-audit` (local only, not pushed)
**Scope:** correctness, security, stability, deployment. No new features, no strategy changes, no live-trading enablement.

---

## 1. Executive Summary

The V11 codebase (V10 signal engine + Sprint 20A–20H SaaS layer) is in **good health**. The build is clean, the container is healthy, all 155 unit tests and all 7 manual e2es pass, and the core signal engine runs uninterrupted. The live-trading safety gate is intact and verified closed (MOCK by default).

The audit found **no critical or high-severity production defects**. It corrected three low-severity issues — one documentation gap, one performance index, and two test-isolation defects in manual e2e scripts — plus confirmed (and documented) that the previously-reported "huge PnL" symptom was transient seed data, not a code bug. No production trading/security/strategy logic was modified.

## 2. Current Version / Commit

- Branch `develop`, 8 commits ahead of `origin/develop` (unpushed, as required).
- Latest: `702101f` Sprint 20H HTML admin page; full 20A–20H lineage present and verified in `git log`.
- Safety tag `pre-v11-audit` set on the pre-audit HEAD for rollback.

## 3. What Passed

| Area | Result |
|------|--------|
| `docker compose build` / `up -d` / `ps` | ✅ all 3 containers healthy |
| Startup logs (error/traceback/import/permission scan) | ✅ clean |
| `compileall app tests` | ✅ exit 0 |
| Route registration (always-on + flag-gated) | ✅ all expected routes present; gated routes mount only when their flag is on |
| Feature flags in `config.py` / `.env` / `.env.example` | ✅ all present & read; safe defaults |
| Auth: bcrypt, JWT HS256+exp, refresh hashing/revoke, suspended-block | ✅ |
| RBAC: admin routes require ADMIN; saas-admin requires cookie; no auth bypass | ✅ |
| Vault: AES-256-GCM, random nonce, no plaintext, last4-only responses, withdrawal reject, passphrase enforce, rollback-safe audit | ✅ |
| Live gate: MOCK unless fully open, `_guard()` defense-in-depth, safety+kill before open | ✅ closed (MOCK) |
| Paper/auto: PnL math (LONG/SHORT), demo balance 10k, DEMO-only execution, safety-first | ✅ |
| DB: 30 tables, no name collision, non-destructive migrations, hot-column indexes | ✅ (one index added) |
| Dashboard `/admin` + `/admin/platform` + saas-admin JSON | ✅ |
| Signal engine: scanner loop, ws cache, regime, short-protection, winrate | ✅ live |
| Unit tests | ✅ **155 passed** |
| Manual e2es (auth/paper/vault/auto/safety/live/admin) | ✅ **7/7 pass** (all MOCK, gate closed) |
| Security scan (eval/exec/subprocess/secret-logging/hardcoded creds) | ✅ clean |

## 4. Critical Issues Found

**None.**

## 5. High-Risk Issues Found

**None.** Specifically verified absent:
- No endpoint returns decrypted or encrypted credentials (responses are `api_key_last4` only).
- No route bypasses authentication; ADMIN-only actions reject FREE users with 403.
- The live-trading gate cannot open without `LIVE_TRADING_ENABLED=true AND MOCK_EXCHANGE_MODE=false`; both default safe and were left unchanged.
- No `eval`/`exec`/`os.system`/`shell=True`/`subprocess`; no secrets logged; no hardcoded production credentials.

## 6. Fixes Applied (all low-severity)

1. **`.env.example` doc gap (Phase 4).** The 20B flag `PAPER_TRADING_ENABLED` (and `DEFAULT_DEMO_BALANCE`) were present in code and `.env` but missing from `.env.example`. Added a documented 20B section. (The earlier "VAULT_MASTER_KEY missing" reading was a false alarm — it is present and intentionally blank, falling back to `SECRET_KEY`, verified to derive a usable 64-char key.)

2. **`signals.status` index (Phase 9).** Status-filtered scans (active-signal summary, dashboard) had no standalone index (only a partial-unique on `symbol`). Added `index=True` on the model and an idempotent `CREATE INDEX IF NOT EXISTS ix_signals_status` to `_SCHEMA_UPGRADES`; applied and verified on the live DB. Non-destructive.

3. **Manual e2e test-isolation (Phase 12).** `e2e_auto_manual.py` and `e2e_safety_manual.py` failed **only** due to shared-dev-DB pollution, not code defects:
   - *auto*: seeded signals with `status="OPEN"`, colliding with `uq_active_signal_symbol` on re-runs. Changed to seed `status="CLOSED"` (the engine ignores status) and added an idempotent pre-purge.
   - *safety*: assumed its first registered user becomes ADMIN, which is false on a populated DB (the FREE user was correctly denied `kill-all` with 403 — *correct* security behavior). Added an explicit `_promote()` to ADMIN.
   Both now pass. **No production code changed for these.**

## 7. Files Changed

```
.env.example                 # + PAPER_TRADING_ENABLED, DEFAULT_DEMO_BALANCE (20B doc)
app/database/models.py       # Signal.status -> index=True
app/database/session.py      # + CREATE INDEX IF NOT EXISTS ix_signals_status
tests/e2e_auto_manual.py     # seed CLOSED + idempotent purge (test isolation)
tests/e2e_safety_manual.py   # explicit ADMIN promote (test isolation)
```
No changes to: signal engine, market regime, short protection, diagnostics, winrate, risk, adapters, vault, live-trading, auto-engine, safety, auth, or any gate.

## 8. Tests Run

- **Unit:** `pytest -q` → **155 passed**.
- **Manual e2e (Postgres, flags: AUTH/PAPER/VAULT/AUTO/SAFETY/LIVE_API/ADMIN on, `MOCK_EXCHANGE_MODE=true`, `LIVE_TRADING_ENABLED=false`, `EMAIL_VERIFICATION_REQUIRED=false`):**
  auth ✅ · paper ✅ · vault ✅ (passphrase enforced, ciphertext-only, secret-wiped) · auto ✅ · safety ✅ (correlated cap, loss-streak, user kill, global kill-all 200 / FREE 403) · live ✅ (all MOCK, no real orders) · admin ✅ (RBAC 403, no secrets, suspend→login-403, self-suspend guard).
- All e2es pre/post-purge their data on the shared dev DB.

## 9. Security Findings

- **Crypto:** AES-256-GCM, HKDF-SHA256-derived key (raw env never used as key), fresh 96-bit `os.urandom` nonce per op, tamper-detecting decrypt. ✔
- **Credential exposure:** `ExchangeAccountOut` and the saas-admin detail view expose only `api_key_last4` + permission flags; repo grep confirms `encrypted_*` appears only in models/crypto/service internals; `get_decrypted_credentials` is not referenced by any router. ✔
- **Withdrawal keys:** rejected at connect (403) and re-quarantined on test if they later gain withdrawal rights; never persisted. ✔
- **Audit durability:** vault and live audits write via an independent `SessionLocal`, surviving request-transaction rollback. ✔
- **Auth:** bcrypt (72-byte safe), HS256 JWT with `exp`, sha256-hashed refresh tokens with revoke, suspended users blocked at login and refresh. ✔
- **Static scan:** no `eval`/`exec`/`os.system`/`shell=True`/`subprocess`; no secret values logged; no hardcoded production credentials. ✔

## 10. Live Trading Gate Verification

- `resolve_adapter()` returns `MockExchangeAdapter` unless `live_trading_enabled AND not mock_exchange_mode AND creds present` — covered by the unit gate matrix.
- Each real adapter (`binance/okx/bybit/bitget`) re-checks via `_guard()` before any network call (defense in depth).
- `live_trading.service.open_position` runs `safety.trading_blocked` (global kill + user kill + lockout) **before** adapter resolution/execution; suspended users are blocked at `get_current_user`.
- Runtime confirmed: `live_gate_open=False`, `mode=MOCK`. **No real orders were placed during the audit.** `LIVE_TRADING_ENABLED` and `MOCK_EXCHANGE_MODE` were not modified.

## 11. Remaining Risks

- **Shared dev DB + live scanner:** manual e2es run against the same Postgres the scanner writes to. Now hardened to be idempotent, but true isolation (a dedicated test DB/schema) would be more robust. Low risk.
- **Monolithic `app/dashboard/server.py` (~270 KB):** not a runtime bug (imported once; bot RSS ~143 MB) but a maintainability/startup-memory consideration. Out of scope to refactor now.
- **Verbose ws price log** every 2 s and **DB pool** (10+20=30 conns) are larger than a 2C4G single-node needs — harmless today, see recommendations.
- **`PnL "Entry=100"` report:** root-caused to transient seed/test data (now absent in all tables); PnL math verified correct and demo/live data is clearly labeled (`mode` MOCK/LIVE, `/api/paper*` namespace). If reintroduced, it would be bad seed data, not a formula bug.

## 12. Recommended Next Steps

- **2C4G deployment:** keep `LIVE_TRADING_API_ENABLED`, `AUTO_TRADE_DEMO_ENABLED`, and `ADMIN_DASHBOARD_ENABLED` **off** unless actively used; run the backtest endpoint sparingly (CPU-heavy in-process); avoid heavy multi-exchange polling. Consider trimming the DB pool to `pool_size=5, max_overflow=10` and moving the ws "price cache updated" log to DEBUG (both optional, deferred — no change made).
- **Test isolation:** consider a dedicated test database for e2es to remove shared-DB coupling.
- **Push:** changes are committed locally only; review then push when ready.

## 13. Rollback Plan

All changes are additive/low-risk and committed in a single audit commit on top of `702101f`.
- **Revert the audit commit:** `git revert <audit-commit>` (keeps history) or `git reset --hard pre-v11-audit` (discards, branch only).
- **DB index:** `DROP INDEX IF EXISTS ix_signals_status;` (safe; no data touched).
- **Container:** `docker compose up -d --build` to redeploy; the prior image still satisfies the schema (the new index is additive and `IF NOT EXISTS`).
- No data migrations are destructive; the `pre-v11-audit` tag marks the exact pre-audit state.
