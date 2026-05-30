# Security Policy — ALPHA RADAR SIGNALS

## Supported Versions

| Version | Supported |
|---------|-----------|
| V3 Enterprise (current) | ✅ Yes |
| V2 and earlier | ❌ No |

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not** open a public GitHub issue.

Report via Telegram to the project admin. Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

We aim to acknowledge reports within 48 hours and resolve critical issues within 7 days.

## Security Measures

### Authentication
- Admin dashboard requires password authentication.
- `DASHBOARD_PASSWORD` is required — startup fails if not set.
- `SECRET_KEY` is required — startup fails if not set.
- Session cookie is `httponly`, `samesite=lax`.

### Input Handling
- All user-supplied values rendered in HTML are escaped via `html.escape()`.
- Affiliate and community URLs are validated: only `http`/`https` schemes accepted.
- Wallet/donation addresses are length-limited and HTML-escaped.

### HTTP Security Headers
All responses include:
- `X-Frame-Options: DENY` — prevents clickjacking.
- `X-Content-Type-Options: nosniff` — prevents MIME sniffing.
- `Referrer-Policy: strict-origin-when-cross-origin`
- `X-XSS-Protection: 1; mode=block`
- `Permissions-Policy: geolocation=(), microphone=(), camera=()`

### Network
- Public API endpoints (`/api/public/*`) return read-only aggregated data only.
- Admin endpoints require authentication cookie.
- Database and Redis are not exposed outside the Docker network.

### Secrets Management
- All secrets are loaded from `.env` (not committed to git).
- `.env` is in `.gitignore`.
- The `.env.example` file contains no real secrets.

### Auto-Trading
- `AUTO_TRADING_ENABLED` is permanently locked to `false` at the code level.
- Live trading is not implemented.

## Hardening Checklist (Pre-Production)

- [ ] Set `DASHBOARD_PASSWORD` to a strong unique password
- [ ] Set `SECRET_KEY` to a random 32-byte hex value
- [ ] Set `POSTGRES_PASSWORD` to a strong unique password
- [ ] Ensure `.env` is not in version control
- [ ] Place the dashboard behind a reverse proxy (nginx/caddy) with TLS
- [ ] Restrict admin access by IP at the firewall/proxy level
- [ ] Enable fail2ban or equivalent for brute-force protection
- [ ] Rotate secrets periodically
