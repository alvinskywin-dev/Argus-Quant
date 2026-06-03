# P11 — Google OAuth Login — Report

**Date:** 2026-06-03
**Status:** ✅ Implemented, tested, image builds. ⏸️ Live manual validation
**STOPPED** pending Google OAuth credentials (rule 10).
**Tests:** 343 passing (16 new in `tests/test_google_oauth.py`).
**Flag:** `GOOGLE_OAUTH_ENABLED=false` by default — inert until configured.

---

## 1. What shipped

### Backend
- **`app/auth/google_oauth.py`** — protocol module (no DB, no persistence):
  `enabled()` gate, `generate_state()` / `validate_state()` (CSRF), scoped
  `authorization_url()`, `exchange_code()` (server-to-server token swap via
  httpx), and `extract_identity()` / `validate_claims()` (aud / iss / exp /
  `email_verified`). The Google access_token is **discarded**; only the
  id_token identity is used and it is **never logged**.
- **Routes** (on the auth router, mounted only when `AUTH_ENABLED=true`):
  - `GET /api/auth/google/login` — sets a short-lived **HTTPOnly, SameSite=Lax**
    state cookie (path-scoped to `/api/auth/google`, `Secure` when the redirect
    is https) and redirects to Google.
  - `GET /api/auth/google/callback` — validates state, exchanges the code, logs
    in / links / creates the user, then redirects to the SPA. Any failure
    redirects to `OAUTH_FAILURE_REDIRECT` with no detail leaked in the URL.
  - `GET /api/auth/google/status` — public; lets the login page decide whether
    to render the button.
- **`service.login_or_link_google()`** — resolution order:
  1. already linked to this Google `sub` → log in;
  2. an email account exists → **link** Google to it (password preserved, so
     email/password login still works);
  3. otherwise **create** a new account: `role=FREE`, `status=ACTIVE`,
     `timezone=UTC`, `is_verified=true`, no usable password.
- **Schema:** `auth_users` gains `provider`, `provider_user_id` (indexed),
  `avatar_url` via idempotent `ALTER TABLE … IF NOT EXISTS` upgrades in
  `session.py` (backward-compatible; existing rows default to `provider='email'`).

### Token handoff
The callback redirects to `OAUTH_SUCCESS_REDIRECT` with the access/refresh
tokens in the **query string** (kept out of the `#` hash route). The SPA's
`consumeOAuthHandoff()` reads them on boot, stores them, then strips them from
the address bar via `history.replaceState` so tokens do not linger in history.

### Frontend (`saas.js`)
- Login page: **"Continue with Google"** button when configured, **"Google
  Login not configured"** when disabled, with email/password kept as fallback.
- Profile: avatar image (Google `picture`), **Login method** (Google / Email),
  and **Email verified** rows.

### Admin
- `list_users` and the user-detail profile now expose `provider` alongside the
  existing `email_verified` (`is_verified`), `timezone`, and `last_login_at`.

### Security
- CSP `img-src` extended to `https://*.googleusercontent.com` for avatars.
- No secret is committed; all Google config is env-only (`.env.example`
  documents it). OAuth state uses a timing-safe comparison.

## 2. Config (`.env.example`)

```
GOOGLE_OAUTH_ENABLED=false
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=          # e.g. http://localhost:8010/api/auth/google/callback
OAUTH_SUCCESS_REDIRECT=/app#/dashboard
OAUTH_FAILURE_REDIRECT=/login?error=oauth_failed
```

## 3. Tests (`tests/test_google_oauth.py`, 16)

Disabled provider · missing config · missing state · invalid state · missing
email · unverified email · audience mismatch · expired token · valid-claim
extraction · **new Google user becomes FREE** · **existing email links
provider** (and password still works) · **duplicate provider_user_id does not
duplicate user** · suspended account blocked.

## 4. Validation performed

| Step | Result |
|------|--------|
| `python -m compileall app tests` | ✅ clean |
| `pytest -q tests/test_google_oauth.py` | ✅ 16 passed |
| `pytest -q` (full) | ✅ 343 passed |
| `ruff check` / `black --check` | ✅ clean |
| Routes mounted (`/api/auth/google/{login,callback,status}`) | ✅ verified |
| `docker compose build bot` | ✅ image built |
| Live OAuth round-trip | ⏸️ **STOPPED** — needs Google credentials |

## 5. STOP — exact setup to finish live validation

The code is complete and inert. To validate the live round-trip, an operator
must provide Google OAuth credentials:

1. **Google Cloud Console** → create/select a project.
2. **APIs & Services → OAuth consent screen** → configure (External; add your
   test users while in "Testing").
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   application type **Web application**.
4. **Authorized redirect URIs** → add exactly:
   `http://localhost:8010/api/auth/google/callback`
   (and the production equivalent, e.g.
   `https://app.argusquant.<tld>/api/auth/google/callback`).
5. Copy the **Client ID** and **Client secret** into `.env` (never commit it):
   ```
   GOOGLE_OAUTH_ENABLED=true
   GOOGLE_CLIENT_ID=<client id>
   GOOGLE_CLIENT_SECRET=<client secret>
   GOOGLE_REDIRECT_URI=http://localhost:8010/api/auth/google/callback
   AUTH_ENABLED=true
   ```
6. `docker compose up -d bot` → open `/app` → **Continue with Google** →
   confirm: new account is FREE/ACTIVE/UTC, profile shows Login method = Google
   and the avatar, and an existing email account links instead of duplicating.

## 6. Commit

`P11 add Google OAuth login`

## 7. Guarantees preserved

No signal/scanner logic touched · no live-trading flag changed · mock/paper
modes intact · all prior routes preserved · no secret committed · not pushed.
Next roadmap phase: **21F — Binance Testnet Validation** (will STOP for testnet
API keys).
