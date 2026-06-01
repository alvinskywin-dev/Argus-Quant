# Admin Dashboard Debug Report — `/admin/platform`

**Symptom:** `/admin/platform` shows "Loading…" forever; the Users and Audit tables
never populate.

**Constraint honored:** trading engine and signal engine untouched. Only the admin
dashboard's data-loading layer (frontend JS embedded in the page) was changed.

---

## 1. Investigation

The platform admin page is a cookie-gated HTML page (`_PLATFORM_ADMIN_HTML` in
`app/dashboard/server.py`) with inline JS that fetches three JSON endpoints:

| Endpoint | Auth | Live result (with cookie) | Without cookie |
| --- | --- | --- | --- |
| `GET /api/saas-admin/overview` | session cookie `alpha_radar_auth=ok` | **200**, ~0.03–0.1s, real data | **401** `{"error":"unauthorized"}` |
| `GET /api/saas-admin/users?limit=500` | session cookie | **200**, ~0.04–0.1s, real data | **401** |
| `GET /api/saas-admin/audit?limit=50` | session cookie | **200**, ~0.01s, real data | **401** |

**The backend is healthy** — every endpoint returns correct data quickly when the
session cookie is present, and a clean `401` when it isn't. The fault is entirely in
the page's frontend fetch/render logic.

### Auth flow
The endpoints use **session-cookie** auth (`_is_logged_in` → `request.cookies['alpha_radar_auth'] == 'ok'`),
**not** JWT. So the fix is on the session side: send the cookie reliably and handle the
auth status codes — no `Authorization` header applies here.

## 2. Root cause

Two defects in the page's inline JS combined to produce permanent "Loading…":

1. **Sequential loaders under one shared `try/catch`.** The bootstrap was:
   ```js
   async function refresh(){ try{ await loadOverview(); await loadUsers(); await loadAudit(); }catch(e){ showErr(e.message); } }
   ```
   `loadUsers()`/`loadAudit()` only run *after* `loadOverview()` resolves. If the first
   call fails, hangs, or the session is expired, the chain aborts and the Users and
   Audit tables are **never touched**, so they keep their initial "Loading…" rows.

2. **`getJSON` had no timeout and used `credentials:'same-origin'`.** A hung/slow
   request (or a deployment where the dashboard is reached via a different origin/proxy
   path, so `same-origin` doesn't attach the cookie) produced either an unresolved
   `fetch` (infinite spinner) or a `401` that hard-redirected to `/login`. There was no
   per-table error state, no `403`/`500` differentiation, and `r.json()` would throw on
   a non-JSON proxy error body — any of which left the page silently stuck.

## 3. Fix applied

**File:** `app/dashboard/server.py` (the inline `<script>` of `_PLATFORM_ADMIN_HTML` only).

- **`getJSON` hardened:**
  - `credentials:'include'` (reliably sends the session cookie, incl. proxied origins).
  - `AbortController` **12s timeout** → a hanging request now throws instead of spinning forever.
  - Tolerant JSON parse (non-JSON error bodies no longer throw a confusing `SyntaxError`).
  - Throws a status-aware `HttpError(status, message)`.
- **Status-aware messages (requirement 6):** `authMsg()` maps
  `401 → "Admin login required…"`, `403 → "Admin permission required…"`,
  `5xx → "Backend error: …"`, plus network/timeout text.
- **Per-region error states (requirement 7):** each loader (`loadOverview`,
  `loadUsers`, `loadAudit`) now self-catches and renders a **visible error** into its
  own area — the Overview cards show an "unavailable" card, the Users/Audit tables show
  an error row (`errRow`) with a "Sign in →" link on 401. No region can stay on
  "Loading…" indefinitely.
- **Independent loading:** `refresh()` now runs all three via
  `Promise.allSettled([...])`, so one failing/slow call can never block the others.
- `viewUser` / `setStatus` error handlers now use the same status-aware messaging.
- Removed the silent `location.href='/login'` auto-redirect on 401 in favor of an
  in-page "Admin login required" message + sign-in link.

## 4. Endpoint status (post-fix, verified live)

- `/admin/platform` → **302 → /login** without cookie; **200** with cookie.
- `/api/saas-admin/overview|users|audit` → **200** (fast) with cookie; **401** without.
- Served page now contains `credentials:'include'`, `AbortController`,
  `authMsg`/`errRow`, `Promise.allSettled`; the old `credentials:'same-origin'` is gone.
- Container rebuilt and **healthy**; log scan clean (no errors/tracebacks).

## Affected files

- `app/dashboard/server.py` — inline JS of `_PLATFORM_ADMIN_HTML` (data-loading layer only).

## Result

A failed, slow, or unauthorized request now produces an immediate, readable message in
the affected region (and a sign-in link for 401), and the three tables load
independently — eliminating the permanent "Loading…" state. Trading and signal engines
were not modified.
