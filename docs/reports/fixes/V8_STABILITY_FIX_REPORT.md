# V8 Stability Fix Report

**Date:** 2026-05-31  
**Branch:** develop

---

## Issues Addressed

### 1. Docker Log Permission Bug

**Root cause:** `app/utils/logger.py` called `logger.add()` for every file sink without any error handling. If the host-mounted `./logs` directory was not writable by the container user (UID 10001), a `PermissionError` would propagate uncaught and crash the app at import time.

**Fix (`app/utils/logger.py`):**
- Extracted file sink setup into `_add_file_sink()` helper that wraps `logger.add()` in `try/except (PermissionError, OSError)`.
- Also wrapped `Path.mkdir()` in a try/except for the same reason.
- If any file sink fails, a warning is printed to stderr and the app continues with stdout-only logging.
- The stdout sink is always added first and is never guarded — the app will always log to stdout.

**Note:** The `./logs` directory is currently owned by UID 10001 (confirmed via `ls -la`), so the immediate runtime error was not reproducible. The logger hardening prevents it from ever crashing the app in future deployments.

---

### 2. Malformed HTML Tags in Landing Page

**Root cause:** Inspected `app/dashboard/server.py` with `grep -Pn "< /(span|div|section|a)"`.  
**Finding:** No malformed tags found in the current codebase — already clean.  
**Action:** No changes required.

---

### 3. Missing `/api/system/metrics` Endpoint

**Root cause:** Frontend and monitoring configs referenced `/api/system/metrics`, but no such route existed in `app/dashboard/server.py`. All requests returned `{"detail": "Not Found"}`.

**Fix (`app/dashboard/server.py`):**
- Added `GET /api/system/metrics` endpoint (Option A).
- Queries the DB for MTF signal counts (total / open / closed / wins).
- Returns the exact schema requested:

```json
{
  "ok": true,
  "signals_total": 72,
  "open_signals": 31,
  "closed_signals": 34,
  "winrate_closed": 41.2,
  "universe": 187,
  "updated_at": "2026-05-31T01:03:50.248347+00:00"
}
```

---

### 4. Repository Cleanliness

**Finding:** `git status` returned no untracked files. The garbage docker-output files (`=`, `CACHED`, `[`, `[internal]`, etc.) were not present.  
**Action:** No changes required.

---

## Files Changed

| File | Change |
|------|--------|
| `app/utils/logger.py` | Wrapped all file sink setups in `try/except`; graceful stdout fallback |
| `app/dashboard/server.py` | Added `GET /api/system/metrics` endpoint |

---

## Validation Results

### Compile Validation (in container)
```
docker compose exec bot python -m py_compile \
  app/config.py app/main.py app/dashboard/server.py \
  app/utils/logger.py app/scanner/scanner.py app/risk/levels.py
→ ALL OK
```

### Docker Status
```
signals-bot        healthy   0.0.0.0:8010->8010/tcp
signals-postgres   healthy   5432/tcp
signals-redis      healthy   6379/tcp
```

### Route Validation
| Endpoint | Status |
|----------|--------|
| `GET /` | ✅ 200 HTML |
| `GET /health` | ✅ 200 HTML |
| `GET /api/health` | ✅ 200 JSON `ok: True` |
| `GET /api/system/metrics` | ✅ 200 JSON (new) |
| `GET /api/public/stats` | ✅ 200 JSON |
| `GET /api/public/performance-center` | ✅ 200 JSON |
| `GET /api/public/market-radar` | ✅ 200 JSON |
| `GET /api/public/setup-library` | ✅ 200 JSON |

### Log Permission Check
```
docker logs signals-bot --since=2m | grep -Ei "permission|traceback|exception|syntaxerror|importerror"
→ (no output — no errors)
```

---

## Remaining Risks

- **Log file permissions on fresh deploy:** If a new host deploys without pre-creating `./logs` owned by UID 10001, the logger will fall back to stdout instead of crashing. Operators should run `mkdir -p logs && chown 10001:10001 logs` on initial setup. This is documented behavior.
- **No new features added** — scanner, strategy thresholds, Telegram, and landing page are unchanged.
