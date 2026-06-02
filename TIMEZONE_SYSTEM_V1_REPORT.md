# Timezone System V1 — Implementation Report

**Date:** 2026-06-02
**Branch:** `feature/timezone-system-v1` (off `develop`)
**Scope:** Platform-wide, consistent timezone handling. **Display + serialization
only.** No changes to trading logic, scanner, signal scoring, live-trading gates,
or exchange adapters. DB storage stays UTC; `created_at` / `updated_at` meaning
is unchanged.

---

## 1. Problem summary

Timestamps were rendered inconsistently: some pages showed server UTC, some
browser local time (`toLocaleString`), and signal/trade rows used a terse
`MM-DD HH:mm` server-formatted string (e.g. `06-01 06:06`) with no zone label.
There was no per-user timezone preference and no admin UTC/User-Time control.

## 2. Architecture decision

- **Database:** UTC only (already true — verified: every write uses
  `datetime.now(timezone.utc)` / `func.now()`; no change made).
- **API:** returns UTC ISO with explicit offset. Existing preformatted keys kept
  for back-compat; UTC-ISO fields **added** alongside (e.g. `time_iso`,
  `opened_iso`).
- **Profile:** each user has a `timezone` preference (supported IANA zone),
  default `UTC`.
- **Frontend:** all timestamps render through centralized helpers using
  `Intl.DateTimeFormat` with the user's zone, 24-hour, zone-labelled.
- **Admin:** UTC ↔ User-Time toggle; legacy admin defaults to UTC.

Supported zones: `UTC, Europe/London, Asia/Phnom_Penh, Asia/Ho_Chi_Minh,
America/New_York, America/Los_Angeles`.

## 3. DB migration

Non-destructive, applied at startup (`app/database/session.py` upgrade list):
```sql
ALTER TABLE auth_users ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) DEFAULT 'UTC';
UPDATE auth_users SET timezone = 'UTC' WHERE timezone IS NULL;
```
Model: `AuthUser.timezone` (`String(64)`, default `'UTC'`, `server_default='UTC'`).
No drops, no data loss; existing users default to UTC.

## 4. Backend utilities — `app/utils/timezone.py`

`SUPPORTED_TIMEZONES`, `DEFAULT_TIMEZONE`, and: `utc_now`, `ensure_utc`
(naive→UTC), `normalize_utc_iso` (→ `...+00:00`, accepts `Z`/naive/None),
`is_supported_timezone`, `safe_timezone`, `to_user_timezone`,
`format_datetime_for_timezone` (`01 Jun 2026 13:00:00 Europe/London`),
`format_short_datetime_for_timezone` (`01 Jun 18:06 UTC`). All None-safe; bad
zones degrade to UTC; uses `datetime.timezone.utc` + `zoneinfo.ZoneInfo`.

## 5. API changes

- `GET /api/auth/me` → now includes `timezone`.
- `GET /api/auth/timezones` → supported list + default (for the picker).
- `PUT /api/auth/timezone` `{ "timezone": "Asia/Phnom_Penh" }` → auth required;
  validates against the supported list (**400** otherwise); persists; returns the
  updated user. Arbitrary IANA strings are rejected.
- Admin (`/api/admin/users`, `/api/admin/users/{id}`, reused by
  `/api/saas-admin/*`) → user payloads include `timezone`.
- Signal/position rows in `/api/dashboard` and OI/funding feeds gained
  `time_iso` / `opened_iso` (UTC ISO) **alongside** the existing keys — no
  contract broken.

## 6. Frontend changes — `app/dashboard/static/saas/saas.js`

Centralized helpers (no scattered `toLocaleString`): `getDisplayTimezone`,
`formatDateTime`, `formatShortDateTime`, `formatDateOnly`, `formatTimeOnly`,
`timeAgoWithTooltip`, `setUserTimezone`, plus `SUPPORTED_TIMEZONES`,
`currentUserTimezone`, `adminTimeMode`. Legacy `when`/`shortTime` now delegate to
these (so every existing call site became zone-aware). Format: 24-hour, zone
label appended; falls back to UTC ISO if `Intl` fails. `currentUserTimezone` is
set from `ME.timezone` at boot.

Applied across SPA pages (dashboard, analytics, paper, live, exchange, auto,
safety, profile): signal rows, live orders/trades, paper trades, auto-execution
history, sessions, last-login, created-at, audit, and the live "Updated …"
ticker.

## 7. Admin time toggle

- **SPA Admin Platform (`#/admin`):** `[UTC] [User Time]` toggle + `Time Mode:`
  badge (persisted in `localStorage`). User rows render in each user's timezone
  (User-Time mode) or UTC; audit rows use the admin's zone, UTC fallback. Detail
  modal shows the user's timezone.
- **Legacy `/admin/platform`:** same toggle + badge, tz-aware `fmtTs(value, tz)`;
  defaults to UTC (dashboard session has no SaaS zone).
- **Legacy `/admin`:** renders UTC with a `Time Mode: UTC` badge; signal table
  now shows full `DD Mon YYYY HH:MM:SS UTC` (via `time_iso`) instead of
  `MM-DD HH:mm`; "Updated"/"Rebuilt" times are UTC-labelled.

## 8. Tests run — `tests/test_timezone_system.py`

`10 passed`. Covers: supported list; invalid-zone rejection; naive→UTC; UTC ISO
normalization (`Z`/naive/None/garbage); London DST (Jan 12:00 / Jun 13:00);
Asia/Phnom_Penh +7 (→19:00); New_York DST (EST 07:00 / EDT 08:00); None-safe +
short format; and the `PUT /api/auth/timezone` reject-unsupported (400) gate +
accept-supported validation.

## 9. Validation result

- `python -m py_compile` (server, auth, admin, models, session, util) — clean.
- `node --check saas.js` — clean.
- **Full suite: 269 passed** (was 259; +10), run in an ephemeral container from
  `futures-signal-bot-bot:latest` with the repo mounted — the live `signals-bot`
  container was **not** touched.
- App boot check: `create_app()` registers `/api/auth/timezone[s]`, `/api/auth/me`
  returns timezone, both admin templates contain the toggle/UTC badge.

## 10. Known limitations

- The live container still runs the previously-built image. Deploying requires
  `docker compose build bot && docker compose up -d bot` — **not performed here**
  (it restarts the live trading service; left to the operator). The startup
  migration adds the `timezone` column on next boot.
- Full DB-backed persistence of `PUT /api/auth/timezone` is exercised by the
  manual e2e flow, not the offline CI suite (no Postgres in CI), which instead
  tests the validation gate directly.
- A few non-user-facing legacy signal-detail strings still use a raw
  `ISO.slice(0,16)` (`YYYY-MM-DD HH:mm`); they are unlabelled but already UTC and
  not on the audited pages.
- `formatDateOnly` / `formatTimeOnly` helpers are provided for future use; not
  every call site needed them.

## 11. Rollback plan

- **Frontend/admin:** revert this branch's commit — `when`/`shortTime`/`fmtTs`
  return to their previous behavior.
- **API:** the added `timezone` / `*_iso` fields are additive; ignoring them is
  harmless. `PUT /api/auth/timezone` is new and can simply be unused.
- **DB:** the column is additive (`DEFAULT 'UTC'`); leaving it is harmless. No
  destructive change to roll back.
- **No flag** gates this (display-only), but reverting the commit fully restores
  prior behavior.
