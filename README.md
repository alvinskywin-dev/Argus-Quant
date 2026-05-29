# AI Futures Signal System

Production-grade AI-powered signal system for Binance USDT-M Futures. Scans all
active pairs in real time, scores setups with a multi-factor model, filters out
low-quality trades, delivers premium signals to Telegram, tracks performance,
and exposes a FastAPI dashboard — all running 24/7 on Docker.

## Features

- Realtime scanner over **all** USDT-M futures pairs (REST + WebSocket)
- Multi-timeframe (1m/5m/15m/1h/4h/1d) analysis
- 18+ indicators (EMA, RSI, MACD, BB, ATR, VWAP, Supertrend, StochRSI, ADX, …)
- Smart Money Concepts: BOS, MSS, liquidity sweep detection
- AI-weighted confidence scoring (0–100%) with risk classification
- Smart filters: chop, fake breakout, low volume, overextension, cooldown
- Auto-delivery to Telegram (groups/channels) with live TP/SL updates
- Full signal tracking: TP1/2/3, SL, PnL, drawdown, winrate, leaderboard
- PostgreSQL storage + Redis cache
- FastAPI dashboard + REST API
- Healthchecks, structured logging, graceful shutdown
- Single-command Docker deployment

## Quick start

```bash
git clone <this-repo> futures-bot
cd futures-bot
cp .env.example .env       # fill in keys
docker compose up -d
docker compose logs -f bot
```

Full setup, Telegram bot creation, Binance API setup, scaling and backup
instructions are in **DEPLOYMENT.md**.
