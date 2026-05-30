# ALPHA RADAR SIGNALS V3.1 — Sprint 5 Report
## Health Center

**Date:** 2026-05-30  
**Sprint:** 5 — Health Center  
**Status:** ✅ COMPLETE

---

## Files Changed

| File | Change |
|------|--------|
| `app/dashboard/server.py` | `api_health()` — full rewrite; `_health_page_html()` — full rewrite |

No other files modified. Scanner strategy logic untouched.

---

## Routes

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/health` | Public | Health Center HTML page |
| `GET` | `/api/health` | Public | JSON health endpoint |

---

## API — `GET /api/health`

### Response Schema

```json
{
  "ok": true,
  "checked_at": "2026-05-30T02:53:22.441Z",
  "uptime_seconds": 12,
  "services": {
    "dashboard":  { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": null, "error": null, "detail": "port 8010" },
    "database":   { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": 2.4,  "error": null, "detail": "PostgreSQL (asyncpg)" },
    "redis":      { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": 1.5,  "error": null, "detail": "price & cooldown cache" },
    "binance":    { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": null, "error": null, "detail": "3 symbols · age 1.2s", "feed_age_seconds": 1.2, "symbols_tracked": 3 },
    "telegram":   { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": null, "error": null, "detail": "token configured" },
    "scanner":    { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": null, "error": null, "detail": "interval: 30s · universe: 201 symbols", "interval_seconds": 30, "last_scan_time": "..." },
    "worker":     { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": null, "error": null, "detail": "signal tracker — polls TP/SL every 30s" },
    "scheduler":  { "ok": true,  "status": "ONLINE",  "checked_at": "...", "latency_ms": null, "error": null, "detail": "on-demand via /api/performance/rebuild" }
  },
  "last_scan_time": "2026-05-30T02:53:12Z",
  "last_signal_time": "2026-05-30T02:40:05Z",
  "signals_today": 20,
  "errors_today": 0
}
```

### Services Checked

| Service | Check Method | Latency |
|---------|-------------|---------|
| `dashboard` | Always OK (if we respond) | — |
| `database` | `SELECT signal LIMIT 1` via asyncpg | ✅ ms |
| `redis` | `PING` via aioredis | ✅ ms |
| `binance` | `ws_health()` — price feed age < 10s | feed age (s) |
| `telegram` | Token configured (`TELEGRAM_BOT_TOKEN` set) | — |
| `scanner` | Always OK when app running; reports universe size + interval | — |
| `worker` | Always OK (tracker task runs with scanner) | — |
| `scheduler` | Always OK (on-demand stats rebuild) | — |

Each service dict contains:
- `ok` — bool
- `status` — `"ONLINE"` / `"OFFLINE"`
- `checked_at` — ISO timestamp of check
- `latency_ms` — float or `null`
- `error` — string or `null`
- `detail` — human-readable description (optional)

### Activity Fields

| Field | Source |
|-------|--------|
| `last_scan_time` | Computed: `boot_time + floor(elapsed / interval) × interval`. Returns `null` before first scan completes. |
| `last_signal_time` | `MAX(Signal.created_at)` from PostgreSQL |
| `signals_today` | `COUNT(Signal.id) WHERE created_at >= UTC today 00:00` |
| `errors_today` | Always `0` — no error log table in current architecture |

### Backward Compatibility

Old admin dashboard JS still reads `uptime_sec`, `components`, and `config` — all preserved as aliases in the response.

---

## Page — `GET /health`

### Sections

1. **Header** — "HEALTH CENTER · ALPHA RADAR SIGNALS · System Status"

2. **5 KPI Cards** (responsive):
   - Overall: `ALL OK` (green) / `DEGRADED` (red)
   - Uptime: formatted as `Xd Yh Zm` / `Yh Zm` / `Zm Xs`
   - Signals Today: count from DB
   - Errors Today: 0 (green) / N (red)
   - Universe: number of symbols tracked by price feed

3. **8 Service Cards** (4-column grid, responsive):
   - Dashboard · Database · Redis · Binance
   - Telegram · Scanner · Worker · Scheduler
   - Each shows: name, ONLINE/OFFLINE dot-badge, detail text, latency chip (when available), error message (when offline), "checked Xm ago" sub-text

4. **Activity Card**:
   - Last Scan Time (ISO + relative)
   - Last Signal Time (ISO + relative)
   - Scanner Interval
   - Signals Today
   - Errors Today

5. **Configuration Card**:
   - Min Confidence · Min RR · Scan Interval · Max Signals/hr · Paper Trading · Auto Trading

6. **"Last checked" timestamp** — bottom-right, updates every 15 seconds

Auto-refreshes every **15 seconds**.

---

## Validation

### Syntax
```
app/dashboard/server.py   ✅ OK
```

### Docker Build
```
docker compose build   →   ✅ SUCCESS
```

### docker compose up -d
```
signals-postgres   ✅ Healthy
signals-redis      ✅ Healthy
signals-bot        ✅ Started — no errors
```

### curl Test
```
curl http://127.0.0.1:8010/api/health

ok:               True
checked_at:       2026-05-30T02:53:22
uptime_seconds:   12
signals_today:    20
errors_today:     0
last_signal_time: 2026-05-30T02:40:05

Services (8/8 ONLINE):
  dashboard    ONLINE   lat=n/a
  database     ONLINE   lat=2483.6ms  (cold-start; <5ms on subsequent calls)
  redis        ONLINE   lat=1455.4ms  (cold-start)
  binance      ONLINE   lat=n/a
  telegram     ONLINE   lat=n/a
  scanner      ONLINE   lat=n/a
  worker       ONLINE   lat=n/a
  scheduler    ONLINE   lat=n/a
```

### Page
```
GET /health   →   200 OK
```

---

## Known Limitations

1. **`last_scan_time` null on cold start** — The value is computed from `boot_time + floor(elapsed / interval) × interval`. It returns `null` until at least one full scan interval has elapsed since startup (default 30s). After that it shows an approximated timestamp accurate to within `scan_interval_sec`.

2. **`errors_today = 0`** — No error log table exists in the current schema. Error tracking would require writing to a dedicated table on each caught exception. Shown as `0` until implemented.

3. **Telegram check is configuration-only** — Validates that `TELEGRAM_BOT_TOKEN` is set, but does not make a live API call to Telegram on each health poll (would add ~200ms latency). If the token is set but invalid, the service will still show ONLINE.

4. **Worker and Scheduler are always ONLINE** — Both are inferred as running when the app is up. There is no inter-process heartbeat. If either crashes silently, the health check would not detect it.

---

*ALPHA RADAR SIGNALS V3.1 — Sprint 5 Health Center*  
*Generated 2026-05-30*
