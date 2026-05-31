# Sprint 20A — User Account + Auth System

**Status:** ✅ Complete · feature-flagged · no changes to protected V10 engines
**Date:** 2026-05-31

Transforms Alpha Radar Signals from a single-admin tool into a multi-user SaaS
platform with email/password identity, JWT sessions, 2FA, and login auditing.
Everything is gated behind `AUTH_ENABLED` and is **off by default**.

---

## What shipped

### New package `app/auth/`
| File | Responsibility |
|------|----------------|
| `security.py` | bcrypt password hashing, JWT access tokens (HS256), opaque refresh/one-time tokens + sha256 hashing, TOTP helpers. Pure functions, no DB. |
| `schemas.py` | Pydantic request/response models (`EmailStr` validated). |
| `service.py` | All business logic over the `auth_*` tables. Raises `AuthError(status, detail)`. |
| `email.py` | Verification/reset email. Logs instead of sending when SMTP is unconfigured. |
| `deps.py` | FastAPI deps: `get_current_user` (Bearer), `require_role(...)`, client IP/device. |
| `router.py` | `/api/auth/*` HTTP layer. |
| `__init__.py` | `setup_auth(app)` — idempotent router + exception-handler mount. |

### Database (new tables, created by `init_db()` `create_all`)
- `auth_users` — id, email (unique), username (unique), password_hash, role, status, is_verified, totp_secret/totp_enabled, **telegram_user_id** (optional bridge), lockout fields, timestamps.
- `auth_sessions` — refresh-token-backed device sessions (stores sha256 of token, ip, device, expiry, revoked).
- `auth_tokens` — one-time VERIFY/RESET tokens (sha256, expiry, used).
- `login_history` — immutable success/failure audit per attempt.

> **Naming note:** a legacy telegram-keyed `users` table already exists
> (`models.py:User`). To avoid breaking it, the SaaS account lives in
> `auth_users` and links back via the optional `telegram_user_id` column.

### API (mounted only when `AUTH_ENABLED=true`)
| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/auth/register` | First account ever becomes `ADMIN`; others `FREE`. |
| POST | `/api/auth/login` | Returns access+refresh; enforces 2FA, lockout, verification, suspension. |
| POST | `/api/auth/refresh` | New access token from a valid refresh token. |
| POST | `/api/auth/logout` | Revokes the session. |
| GET  | `/api/auth/me` | Current user (Bearer required). |
| POST/GET | `/api/auth/verify-email` | Consume VERIFY token. |
| POST | `/api/auth/forgot-password` | Always 200 (no account enumeration). |
| POST | `/api/auth/reset-password` | Consume RESET token; revokes all sessions. |
| POST | `/api/auth/2fa/setup` · `/2fa/enable` · `/2fa/disable` | TOTP enrolment. |
| GET  | `/api/auth/sessions` | Active sessions for the user. |

### Security properties
- **Passwords:** bcrypt (configurable rounds). Uses the `bcrypt` lib directly — `passlib`'s bcrypt-4.x backend is broken and was bypassed.
- **Tokens at rest:** refresh/verify/reset tokens are stored only as sha256 hashes; raw values never persisted.
- **Roles:** `ADMIN` / `PREMIUM` / `FREE` via `require_role(...)`.
- **Account lockout:** N failed logins → temporary lock (`ACCOUNT_LOCKOUT_*`).
- **2FA:** TOTP (pyotp), opt-in, confirmed by code before activation.
- **No enumeration:** forgot-password and login reveal nothing about which emails exist.
- **Auditing:** every attempt recorded in `login_history` with ip + user-agent.

### Live-trading master gate (foundation for 20D–20G)
Added `LIVE_TRADING_ENABLED` (default **false**) to `config.py` and `.env.example`.
Exchange adapters in later sprints must run in MOCK mode and place no real
orders unless this is explicitly true — even with valid user API keys.

---

## Validation
- `docker compose build` — clean.
- Full suite: **92 passed** (80 existing + 12 new `tests/test_auth.py`).
- Route-mount check with `AUTH_ENABLED=true`: all 12 endpoints registered.
- End-to-end against Postgres (`tests/e2e_auth_manual.py`): register→duplicate-reject→bad-login→login→`/me` gating→refresh→logout-invalidates-refresh. Test data cleaned up afterward.

## Config added (`.env.example`)
`AUTH_ENABLED`, `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_TTL_MIN`,
`REFRESH_TOKEN_TTL_DAYS`, `BCRYPT_ROUNDS`, `EMAIL_VERIFICATION_REQUIRED`,
`ACCOUNT_LOCKOUT_THRESHOLD/MINUTES`, `APP_BASE_URL`, `AUTH_ISSUER`,
`SMTP_*`, and the global `LIVE_TRADING_ENABLED`.

## Untouched (per spec)
Signal Engine · Market Regime Engine · Short Protection Layer · Diagnostics ·
Winrate Analyzer — no changes.
