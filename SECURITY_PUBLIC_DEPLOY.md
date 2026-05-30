# SECURITY_PUBLIC_DEPLOY.md

## Required before public launch

1. Set `DASHBOARD_PASSWORD` in `.env` to a long unique password.
2. Do not expose `.env`, logs, database ports, or Redis ports publicly.
3. Keep admin pages under `/login` and `/admin`; the public dashboard must remain read-only.
4. Only use HTTPS URLs for community and affiliate links.
5. Keep `LOG_REJECTION_DETAIL=false` in production to avoid huge logs.
6. Add Nginx/Cloudflare SSL before serious public use.
7. Never enable Binance withdrawal permission for any future auto-trading API feature.
8. Auto-trading must require explicit user consent, encrypted API keys, max loss controls, mandatory stop loss, and emergency stop.

## Public risk disclaimer

Signals are for educational purposes only. Futures trading is high risk. Users are responsible for their own decisions.
