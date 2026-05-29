# ALPHA RADAR SIGNALS — Stability & Scalability Upgrade Guide

**Date:** 2026-05-29

---

## Applied in This Pass

All items in this section were implemented and are live in the current codebase.

### Signal Delivery Reliability
- **Routing fallback**: Any signal now always reaches `TELEGRAM_SIGNAL_CHAT_ID` even without tier-specific chat env vars.
- **Text-only fallback**: If image card generation fails, a text message is sent instead. Signal delivery is never silently lost.
- **Retry with exponential backoff**: Telegram sends retry up to 4 times with proper `RetryAfter`/`TimedOut`/`NetworkError` handling.
- **Pause check**: `broadcast_signal()` reads the DB pause flag before sending.

### Startup Safety
- **Self-diagnostics**: On every boot the system verifies Binance, PostgreSQL, Redis, and Telegram with clear ✅/❌ output. Fails hard on critical errors.
- **Startup report**: Logs active config (timeframes, thresholds, port, universe size) before first scan.

### Observability
- **Per-module logs**: `scanner.log`, `telegram.log`, `database.log`, `websocket.log` plus the existing `app.log` / `errors.log`.
- **Signal lifecycle logging**: Every signal logs `GENERATED → SAVED → BROADCAST` or explains which stage rejected it.
- **`/health`** (with uptime), **`/status`** (config snapshot), **`/metrics`** (Prometheus text format) endpoints.

### Performance
- **Supertrend rewrite**: Pure-numpy inner loop — ~10× faster, pandas 2.x safe.
- **TF weight coverage**: Added `2h`, `6h`, `12h`, `3d`, `1w` to `TF_WEIGHTS` so non-standard timeframes score correctly.

### Docker / Infrastructure
- **Port alignment**: Config default, docker-compose mapping, and healthcheck all use port 8010.
- **HTML/JS fixes**: Dashboard `load()` function no longer crashes on hyphenated variable names.
- **Live leaderboard**: Dashboard sidebar shows dynamic data from the API instead of static HTML.

---

## Recommended Next Steps (Not Yet Implemented)

### Priority 1 — Production Config Tuning

```env
# Current production settings are very restrictive:
MIN_CONFIDENCE=90       # consider 72-82 to see more signals
MAX_SIGNALS_PER_HOUR=1  # consider 6-12 for 24/7 usefulness
SYMBOL_COOLDOWN_MINUTES=180  # 3 hours is long; consider 60-90
```

Run with lower thresholds for 48 hours, review signal quality, then tune up.

### Priority 2 — True WebSocket Price Feed

`ws_engine.py` currently polls 3 REST endpoints every 2 seconds (sequential, not concurrent). Replace with a real Binance WebSocket stream:

```python
# Replace REST polling with:
wss://fstream.binance.com/stream?streams=btcusdt@ticker/ethusdt@ticker/solusdt@ticker
```

Benefits: sub-100ms latency, no rate-limit cost, auto-reconnect built into `websockets` library.

### Priority 3 — Database Migrations with Alembic

Current `create_all` approach skips schema evolution. Add proper migrations:

```bash
alembic init alembic
alembic revision --autogenerate -m "initial"
alembic upgrade head
```

Critical before any production schema change.

### Priority 4 — Missing Database Indexes

Add these for dashboard query performance at scale:

```sql
-- For get_open_signals() used by tracker every 30s
CREATE INDEX IF NOT EXISTS ix_signals_status ON signals(status);

-- For winrate_summary() / leaderboard()
CREATE INDEX IF NOT EXISTS ix_signals_created_status ON signals(created_at, status);

-- For last_signal_for() used by cooldown checker
CREATE INDEX IF NOT EXISTS ix_signals_symbol_side_created_desc 
  ON signals(symbol, side, created_at DESC);
```

### Priority 5 — Signal Expiry

Open signals never close unless TP/SL is hit. Add auto-expiry:

```python
# In tracker.run_forever():
expiry_hours = 72
cutoff = utcnow() - timedelta(hours=expiry_hours)
expired = await repo.expire_old_signals(cutoff)
if expired:
    logger.info(f"expired {expired} stale open signals")
```

### Priority 6 — Prometheus + Grafana Stack

Add `prometheus-client` metrics to the scanner:

```python
from prometheus_client import Counter, Gauge, Histogram

signals_generated = Counter('signals_generated_total', 'Signals generated', ['side'])
signals_broadcast = Counter('signals_broadcast_total', 'Signals sent to Telegram')
scan_duration = Histogram('scan_duration_seconds', 'Full scan cycle duration')
```

Pair with a Grafana dashboard watching `/metrics`.

### Priority 7 — Redis Cache Warm-up on Restart

After restart, the Redis cache is cold. All 200 symbols × 4 timeframes = 800 API calls hit Binance simultaneously. Add a staggered warm-up:

```python
async def _warmup_cache():
    symbols = universe.symbols[:50]  # warm top-50 by volume first
    for sym in symbols:
        for tf in settings.timeframes:
            await fetch_klines(sym, tf, limit=250)
            await asyncio.sleep(0.05)  # 20 req/s
```

### Priority 8 — Telegram Channel Health Check

Periodically verify the bot can post to the configured channels:

```python
async def _verify_chat_access(bot, chat_id: str) -> bool:
    try:
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat_id, bot.id)
        can_post = member.can_post_messages or chat.type == "private"
        return can_post
    except Exception as exc:
        logger.error(f"chat {chat_id} inaccessible: {exc}")
        return False
```

Run this check in `_startup_report()` and alert admin if channels are misconfigured.

### Priority 9 — Docker: Non-Root Volume Permissions

The bot runs as UID 10001. If the host `./logs` directory has different ownership, log writes will fail silently. Add to `docker-compose.yml`:

```yaml
volumes:
  - ./logs:/app/logs:z
  - ./data:/app/data:z
```

And ensure host dirs have correct permissions:
```bash
mkdir -p logs data && chown -R 10001:10001 logs data
```

### Priority 10 — Rate-Limit-Aware Batch Scanning

The scanner sends up to 800 klines requests per scan cycle. With the 12-concurrency semaphore and Redis cache hits, most are served from cache. But after cache expiry or restart, Binance's 2400 weight/minute limit can be hit. Consider:

- Prioritize top-50 symbols by volume for fresh data
- Use Binance batch endpoint where available
- Add a weight-tracking wrapper around `BinanceClient`

---

## Reliability Assessment

| Component | Before | After |
|-----------|--------|-------|
| Signal delivery | ❌ Broken (routing always returned []) | ✅ Reliable with retry |
| Startup | ⚠️ Silent (no checks) | ✅ Self-diagnosing |
| Dashboard API | ❌ Crashes on /api/dashboard | ✅ Fixed |
| Dashboard JS | ❌ Broken load() function | ✅ Working |
| Docker port | ⚠️ Mismatched defaults | ✅ Aligned |
| Log visibility | ⚠️ One log file | ✅ Per-subsystem |
| Supertrend | ⚠️ Pandas 2.x warning | ✅ Numpy |
| Telegram retry | ❌ None | ✅ Exponential backoff |
| Image fallback | ❌ None | ✅ Text fallback |
