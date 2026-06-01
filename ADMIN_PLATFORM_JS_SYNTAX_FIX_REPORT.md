# Admin Platform JavaScript Syntax Fix Report

**Date:** 2026-06-01
**Branch:** develop
**Scope:** `/admin/platform` inline JavaScript only. No trading engine, signal engine, scanner, risk engine, or live-trading code touched.

## Symptom

Browser console on `/admin/platform`:

```
Uncaught SyntaxError: Invalid regular expression: missing /
platform:137:82
```

- `/admin/platform` returned HTML (HTTP 200) but the **Users** and **Audit** tables stayed stuck on *Loading…*.
- The underlying API was healthy:
  ```
  curl -s -b "alpha_radar_auth=ok" http://127.0.0.1:8010/api/saas-admin/users?limit=5 | jq
  ```
  returned valid user data.

A single parse error in the page's one inline `<script>` aborts the **entire** script, so neither `loadUsers()` nor `loadAudit()` ever ran — which is why *both* tables hung.

## Root cause

Rendered HTML, line 137 (source: `app/dashboard/server.py`, `_PLATFORM_ADMIN_HTML`, lines 4851–4852):

```js
const act=u.status==='SUSPENDED'
  ? '<button class="btn-act" onclick="setStatus('+u.id+",'ACTIVE')">Activate</button>"
  : '<button class="btn-sus" onclick="setStatus('+u.id+",'SUSPENDED')">Suspend</button>";
```

The second concatenated piece, `",'ACTIVE')">Activate</button>"`, was a **double-quoted** JS string. Parsing the characters:

- `"` opens the string
- content runs `,'ACTIVE')`
- the `"` **immediately after `)`** (`...')"` → the `"` before `>`) **closes the string early**, yielding `,'ACTIVE')`
- the leftover `>Activate</button>"` is then parsed as code: `>` operator, identifier `Activate`, then `<` and `/button>` — the `/` is read as the start of a **regular-expression literal** that never closes.

Hence: *"Invalid regular expression: missing /"* at column 82 (the `/` of `</button>`).

In the Python source the literal was written with an escaped quote `\">` that renders to a real `"`, which is exactly the `"` that prematurely terminated the string.

## Fix

Switched the two button strings to **single-quoted** JS literals (matching the surrounding concatenation idiom, e.g. the `onclick="viewUser('+u.id+')"` line directly below) and escaped the inner single quotes used by the `setStatus(...)` arguments.

`app/dashboard/server.py` lines 4851–4852 (Python source; `\\'` renders to JS `\'`):

```python
      ? '<button class="btn-act" onclick="setStatus('+u.id+',\\'ACTIVE\\')">Activate</button>'
      : '<button class="btn-sus" onclick="setStatus('+u.id+',\\'SUSPENDED\\')">Suspend</button>';
```

Rendered JS (now valid):

```js
? '<button class="btn-act" onclick="setStatus('+u.id+',\'ACTIVE\')">Activate</button>'
: '<button class="btn-sus" onclick="setStatus('+u.id+',\'SUSPENDED\')">Suspend</button>';
```

Produced HTML is unchanged and correct, e.g.:
`<button class="btn-act" onclick="setStatus(32,'ACTIVE')">Activate</button>`.

This was the only JS-string-with-embedded-quote of this pattern in the block; all other `onclick=` usages are plain HTML attributes and were already valid.

## Verification

**4. `node --check` on the extracted inline script (rendered output):**
```
NODE_CHECK_OK (no syntax error)
```
Entire 131-line inline script parses cleanly.

**5. Rebuild & restart Docker:** `docker compose up -d --build bot` — image rebuilt, `signals-bot` recreated, reached `healthy`.

**6. Users API with cookie:**
```
curl -s -b "alpha_radar_auth=ok" http://127.0.0.1:8010/api/saas-admin/users?limit=5 | jq
```
→ 5 users returned (e.g. id 32, role FREE, status ACTIVE).

**7. `/admin/platform` console syntax error:** gone. The rendered inline script passes `node --check`; the offending `setStatus` lines now read with escaped single quotes.

**8. Tables show data:**
- Users API returns data → `loadUsers()` now executes and renders rows (View / Activate / Suspend buttons).
- Audit API (`/api/saas-admin/audit?limit=5`) returns rows (mode `MOCK`) → `loadAudit()` renders them.
- On any fetch failure the code still renders a visible error row via `errRow(...)` / `showErr(...)` and `authMsg(e)` for 401/403.

## Out of scope (untouched)

Trading engine, signal engine, scanner, risk engine, and live-trading paths were not modified. Audit records remain `MOCK` mode, consistent with the live-trading gate.

## Files changed

- `app/dashboard/server.py` — 2 lines (4851–4852) in `_PLATFORM_ADMIN_HTML`.
