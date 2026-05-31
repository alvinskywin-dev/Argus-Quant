# Sprint 20C — Exchange API Vault

**Status:** ✅ Complete · feature-flagged (`EXCHANGE_API_VAULT_ENABLED`) · protected V10 engines untouched
**Date:** 2026-05-31

Securely stores per-user exchange API credentials for Binance / OKX / Bybit /
Bitget. Secrets live **only** as AES-256-GCM ciphertext, withdrawal-enabled
keys are rejected and never persisted, and every action is audit-logged.

---

## What shipped

### New package `app/exchange_vault/`
| File | Responsibility |
|------|----------------|
| `crypto.py` | AES-256-GCM. Key derived from the master secret via HKDF-SHA256; fresh 96-bit nonce per encryption; token = base64(nonce‖ciphertext‖tag). No plaintext ever leaves this module. |
| `adapters.py` | `validate()` permission-check interface. `MockExchangeValidator` (used when `MOCK_EXCHANGE_MODE=true`) infers permissions offline — no network, no risk. Real Binance/OKX/Bybit/Bitget adapters arrive in 20F/20G. |
| `service.py` | connect / test / disconnect / list, withdrawal rejection, independent audit logging. Raises `VaultError`. |
| `schemas.py` | Pydantic models — the account view never carries secrets. |
| `router.py` | `/api/exchange/*` HTTP layer (auth-gated). |
| `__init__.py` | `setup_exchange_vault(app)` — idempotent mount. |

### Database
- `exchange_accounts` — `user_id`, `exchange`, `label`, `encrypted_api_key`, `encrypted_api_secret`, `encrypted_passphrase` (all ciphertext, nullable), `api_key_last4` (display hint only), `status`, `can_trade/can_futures/can_withdraw`, `last_error`, `last_test`. Unique on (user_id, exchange, label).
- `exchange_audit_log` — immutable CONNECT/TEST/DISCONNECT/REJECT trail with result + ip.

### API (mounted only when `EXCHANGE_API_VAULT_ENABLED=true`; requires a 20A token)
| Method | Path | Behaviour |
|--------|------|-----------|
| POST | `/api/exchange/connect` | Validate permissions → **reject if withdrawal-enabled** (403, not stored) or missing trade/futures (400) → else encrypt + store `CONNECTED`. |
| POST | `/api/exchange/test` | Decrypt → re-validate → update status. A key that *gained* withdrawal rights is quarantined (`ERROR`). |
| POST | `/api/exchange/disconnect` | Wipe all ciphertext columns, set `DISCONNECTED`. No secret material retained. |
| GET | `/api/exchange/accounts` | List the user's accounts — **never** returns secrets (only `api_key_last4`, status, perms). |

### Security properties (spec checklist)
- **AES-256 encryption** ✓ (GCM, HKDF-derived key).
- **Per-user secrets** ✓ (`user_id` FK, scoped queries).
- **No plaintext storage** ✓ (verified in e2e: DB holds only ciphertext that decrypts back).
- **Validate trading + futures permission** ✓ / **reject withdrawal APIs** ✓ (never persisted).
- **Test / Disconnect** ✓. **Audit logs + IP tracking** ✓.
- OKX/Bitget passphrase required and validated.

---

## Fix during build
Rejection audit rows were initially written on the request session, which
**rolls back** when `VaultError` propagates — silently dropping exactly the
REJECT/FAIL records we most need. `_audit()` now writes in its own session and
commits immediately, independent of the caller's transaction. Verified: a
withdrawal-key rejection and a missing-passphrase rejection both persist.

## Validation
- `docker compose build` — clean.
- Full suite: **114 passed** (103 prior + 11 new `tests/test_exchange_vault.py`: crypto roundtrip, non-deterministic ciphertext, tamper detection, mock permission inference).
- Route-mount check: the 4 spec endpoints registered.
- End-to-end vs Postgres (`tests/e2e_vault_manual.py`): withdrawal key → **403, not persisted**; OKX without passphrase → 400; trade+futures key → `CONNECTED`; **DB stores ciphertext only** (plaintext absent, decrypts back); test → CONNECTED; accounts listing carries no secrets; disconnect → ciphertext wiped. Audit log shows all 5 actions incl. both rejections. Test data cleaned up.

## Config (`.env` / `.env.example`)
`EXCHANGE_API_VAULT_ENABLED`, `MOCK_EXCHANGE_MODE`, `VAULT_MASTER_KEY`
(blank → derived from `SECRET_KEY`; rotating it invalidates stored keys).

## Notes / follow-ups
- Real exchange validation requires `MOCK_EXCHANGE_MODE=false` + the 20F/20G adapters; until then `get_validator` raises rather than risk a wrong call.
- `service.get_decrypted_credentials()` is the internal-only accessor the 20D auto engine will use; it is never wired to a response model.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer — no changes.
