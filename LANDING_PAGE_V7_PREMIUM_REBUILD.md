# ALPHA RADAR SIGNALS V7 Premium Landing Page Rebuild

Rebuilt the public homepage from scratch to match the selected premium crypto SaaS mockup.

## Goals
- Premium CoinGlass / TradingView visual quality
- Large hero with radar scanner, logo, feature icons and CTAs
- Stats strip, partner exchanges, Telegram CTA, signals, performance, donations and footer
- Hide weak/negative headline performance until enough verified data exists
- Do not touch scanner, backtest, database, Telegram bot or trading logic

## Validation
Run:

```bash
docker compose build
docker compose up -d
curl -s http://127.0.0.1:8010/ | grep -E "AI-POWERED|Trusted Partner Exchanges|JOIN 12,000|Support Alpha Radar"
```
