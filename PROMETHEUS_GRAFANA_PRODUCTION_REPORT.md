# Prometheus + Grafana Production Monitoring — Report

**Date:** 2026-06-03
**Status:** ✅ Metrics expanded, monitoring stack added (opt-in), configs validated.
**Tests:** 370 passing (1 new metrics-content assertion).
**No default behaviour change** — the monitoring stack is a separate compose
overlay; `docker compose up` is unchanged.

---

## 1. Expanded application metrics (`/metrics`, Prometheus text format)

Built on the Phase 6 metrics foundation. New series:

| Metric | Type | Source |
|--------|------|--------|
| `alpha_radar_live_gate_open` | gauge | `live_gate_open()` (1=real, 0=mock) |
| `alpha_radar_ws_reconnects_total` | counter | ws price-loop backoff path |
| `alpha_radar_telegram_send_failures_total` | counter | Telegram transport retry-exhaustion |
| `alpha_radar_live_positions_open` | gauge | `live_positions` (best-effort scrape query) |
| `alpha_radar_unsafe_positions` | gauge | `live_positions.requires_review` |
| `alpha_radar_paper_positions_open` | gauge | `paper_account_positions` |
| `alpha_radar_reconciliation_issues_unresolved` | gauge | `reconciliation_issues.resolved=false` |
| `alpha_radar_order_failures_pending` | gauge | `order_failures.final_state='PENDING'` |
| `alpha_radar_signals_total` | counter | `signals` (MTF strategy) |
| `alpha_radar_db_up` / `alpha_radar_redis_up` | gauge | `SELECT 1` / Redis `ping` |

Plus the existing universe size, uptime, ws-ok, and HTTP request/latency/error
families. The DB-derived series run as **best-effort scrape-time queries** — any
single failing query (or an unreachable DB/Redis) is skipped, so `/metrics`
never errors and stays scrapeable during an outage (it reports `db_up 0`).

Instrumentation was added only at transport chokepoints (ws reconnect, Telegram
retry) and via scrape-time reads — **no scanner or signal logic was touched**.

## 2. Monitoring stack (opt-in overlay)

`docker-compose.monitoring.yml` adds three services, all bound to `127.0.0.1`:

- **prometheus** (`:9090`) — scrapes `bot:8010/metrics` + node-exporter every 30s,
  15-day retention. Config: `monitoring/prometheus.yml` (promtool-validated).
- **grafana** (`:3000`) — admin password from `GRAFANA_ADMIN_PASSWORD`
  (`.env`), sign-up + anonymous disabled, Prometheus datasource +
  dashboards auto-provisioned.
- **node-exporter** (`:9100`) — host/container resource metrics.

Run alongside the app (shares the project network):
```
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d \
    prometheus grafana node-exporter
```

### Grafana dashboards (`monitoring/grafana/dashboards/`, auto-provisioned)
1. **System Health** — DB/Redis/WS up, uptime, HTTP rate/latency/5xx, WS reconnects.
2. **Trading Safety** — live gate, unsafe positions, unresolved reconciliation, pending order failures.
3. **Signal Engine** — universe size, total signals, signal rate, WS feed.
4. **Live Execution** — open live positions, unsafe positions, live gate, order failures.
5. **SaaS Users** — open paper/live positions, Telegram failures.

## 3. Security

- Grafana admin password is env-driven (`GRAFANA_ADMIN_PASSWORD`), sign-up and
  anonymous access disabled, all three services bound to localhost only. Do not
  expose Grafana publicly without the reverse proxy + auth (next phase).
- No secret committed; `.env.example` documents `GRAFANA_ADMIN_USER/PASSWORD`.

## 4. Validation

| Step | Result |
|------|--------|
| `python -m compileall app` | ✅ clean |
| `pytest -q` (full) | ✅ 370 passed |
| `ruff check` / `black --check` | ✅ clean |
| `/metrics` returns 200 with the new families (DB down → `db_up 0`, resilient) | ✅ verified |
| `docker compose ... config` (app + monitoring overlay) | ✅ valid |
| `promtool check config monitoring/prometheus.yml` | ✅ SUCCESS |
| 5 Grafana dashboards | ✅ valid JSON |
| `docker compose build bot` | ✅ image built |

## 5. Commit

`Add production Prometheus and Grafana monitoring`

## 6. Guarantees preserved

Default `docker compose up` unchanged (monitoring is a separate overlay) · no
signal/scanner change · /metrics resilient to DB/Redis outage · all routes
preserved · no secret committed · not pushed.

Next roadmap phase: **Cloudflare reverse proxy + Nginx SSL** (docs + config; no
external credentials required, no real domain needed in code).
