# Deploying ARGUS QUANT behind Cloudflare + Nginx (HTTPS)

This guide puts the bot dashboard (`127.0.0.1:8010`) behind HTTPS using
**Cloudflare DNS + Nginx** with SSL mode **Full (strict)** — the recommended
production setup. No real domain is baked into the app; everything below is
configured at the edge / origin.

> Companion config: [`infra/nginx/argus-quant.conf`](../../infra/nginx/argus-quant.conf).

---

## 1. Options (recommended: A)

| Option | When |
|--------|------|
| **A. Cloudflare DNS + Nginx on the VPS** *(recommended)* | Full control, real origin cert, rate limiting at the origin. |
| B. Cloudflare Tunnel (`cloudflared`) | No public inbound ports; good for hosts without a stable public IP. |
| C. Cloudflare Worker reverse proxy | Quick tests only — not for production. |

## 2. DNS (Cloudflare)

1. Add the domain to Cloudflare; set the registrar nameservers to Cloudflare's.
2. Create proxied (orange-cloud) records:
   - `app.<domain>` → A → your server IP
   - `api.<domain>` → A → your server IP (optional; same origin)

## 3. SSL/TLS mode — Full (strict)

Cloudflare → **SSL/TLS → Overview → Full (strict)**. This requires a valid
certificate on the origin, so:

1. Cloudflare → **SSL/TLS → Origin Server → Create Certificate** (covers
   `*.<domain>`, `<domain>`).
2. Save the cert + key on the VPS:
   ```
   /etc/ssl/cloudflare/argus-quant-origin.pem
   /etc/ssl/cloudflare/argus-quant-origin.key   # chmod 600
   ```
3. Enable **Always Use HTTPS** and **HSTS** (Edge Certificates).

## 4. Nginx

```bash
sudo cp infra/nginx/argus-quant.conf /etc/nginx/sites-available/argus-quant.conf
sudo ln -s /etc/nginx/sites-available/argus-quant.conf /etc/nginx/sites-enabled/
# edit server_name + ssl_certificate paths
sudo nginx -t && sudo systemctl reload nginx
```

The provided config gives you:
- HTTP→HTTPS redirect; TLS 1.2/1.3 with the Cloudflare Origin cert.
- Reverse proxy to `127.0.0.1:8010` with `Host` / `X-Forwarded-*` /
  `X-Request-ID` passthrough (correlation IDs flow end-to-end).
- **Rate limits**: `/api/auth/*` at 10 r/min (brute-force defence), general
  `/api/*` at 120 r/min, each with a burst.
- `client_max_body_size 1m` (orders/signals are tiny JSON).
- WebSocket upgrade support on `/api/` and `/`.
- Security headers (defence-in-depth on top of the app's CSP), incl. HSTS.
- Long-cache for `/static/`; SPA + `/admin` + everything else proxied.

### Restore the real client IP
Behind Cloudflare, `$remote_addr` is a Cloudflare IP. The config reads
`CF-Connecting-IP`; for `X-Forwarded-For` trust, add `set_real_ip_from` for the
[Cloudflare IP ranges](https://www.cloudflare.com/ips/) so the app's per-request
logs and rate limits see the true client.

## 5. Firewall checklist (VPS)

- Allow **80/443** inbound only (Cloudflare reaches the origin on these).
- **Do not** expose `8010` publicly — bind the app to `127.0.0.1` or block
  `8010` at the firewall; only Nginx talks to it.
- Monitoring (`9090/3000/9100`) stays bound to `127.0.0.1` (see
  `docker-compose.monitoring.yml`); reach Grafana via an SSH tunnel or a
  separate authenticated proxy — never expose it publicly unprotected.
- Optionally restrict inbound 80/443 to Cloudflare IP ranges (ufw/iptables).

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## 6. Verify

```bash
curl -I https://app.<domain>/            # 200, security headers present
curl -s https://app.<domain>/api/live/status   # {"mode":"MOCK",...}
curl -s https://app.<domain>/metrics | head    # alpha_radar_* series
# auth rate limit (expect 429 after the burst):
for i in $(seq 1 40); do curl -s -o /dev/null -w "%{http_code} " \
  https://app.<domain>/api/auth/login -X POST; done
```

## 7. Notes

- The app already sends a strict CSP and correlation IDs; Nginx adds HSTS and
  re-asserts the framing/nosniff headers at the edge of the origin.
- Keep `LIVE_TRADING_ENABLED=false` until you have completed the testnet
  validation and the gated pilot review — TLS does not change the trading gate.
