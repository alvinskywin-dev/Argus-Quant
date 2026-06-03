# Cloudflare + Nginx SSL Deployment — Report

**Date:** 2026-06-03
**Status:** ✅ Config + guide delivered and syntax-validated. Infra/docs only —
no app code changed, no real domain baked in.

---

## 1. What shipped

- **`infra/nginx/argus-quant.conf`** — production reverse-proxy config:
  - HTTP→HTTPS redirect; TLS 1.2/1.3 terminating a **Cloudflare Origin CA**
    cert (SSL mode **Full (strict)**); HTTP/2 via the modern `http2 on;`.
  - Reverse proxy to `127.0.0.1:8010` with `Host` / `X-Forwarded-*` /
    `X-Request-ID` passthrough (correlation IDs flow end-to-end) and
    `CF-Connecting-IP` real-IP restore.
  - **Rate limits**: `/api/auth/*` 10 r/min (brute-force defence), general
    `/api/*` 120 r/min, each with a burst.
  - `client_max_body_size 1m`; WebSocket upgrade support on `/api/` and `/`.
  - Security headers (HSTS + framing/nosniff/referrer) layered on top of the
    app's CSP; long-cache for `/static/`; SPA/`/admin`/`/api` all routed.
- **`docs/deployment/cloudflare-nginx-ssl.md`** — end-to-end guide: option
  comparison (recommended: Cloudflare DNS + Nginx), DNS records, **Full
  (strict)** + Origin CA cert steps, Nginx install/reload, real-client-IP
  restore, a **firewall checklist** (allow 80/443 only; never expose 8010 or
  the monitoring ports), and verification curls (incl. the auth rate-limit 429
  check).

## 2. Validation

| Step | Result |
|------|--------|
| `nginx -t` (config in `http{}` with a test cert) | ✅ syntax ok / test successful |
| No deprecated directives | ✅ (switched to `http2 on;`) |
| App code unchanged | ✅ (infra + docs only) |
| `pytest -q` (unchanged from prior phase) | ✅ 370 passed |

## 3. Security highlights

- Origin only reachable on 80/443; the app's `8010` stays bound to localhost /
  firewalled — only Nginx talks to it.
- Monitoring stack (Prometheus/Grafana/node-exporter) remains localhost-bound;
  the guide says to reach Grafana via SSH tunnel / authenticated proxy, never
  exposed unprotected.
- Auth endpoints are rate-limited at the edge of the origin; HSTS forces HTTPS.
- TLS does **not** change the trading gate — the guide reiterates keeping
  `LIVE_TRADING_ENABLED=false` until testnet + pilot review are complete.

## 4. Commit

`Add Cloudflare and Nginx SSL deployment guide`

## 5. Guarantees preserved

No app code or routes changed · no real domain in code (placeholders +
comments) · no secret committed · not pushed.

Next roadmap phase: **Multi-user Live Beta** (gated foundation; no external
credentials required to scaffold).
