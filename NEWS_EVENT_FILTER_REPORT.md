# Sprint 22E — News / Macro Event Risk Filter

## Goal
Avoid opening new entries during abnormal macro / news volatility (CPI, FOMC,
Fed, NFP, BTC-ETF headlines, token unlocks, Binance listing/delisting).

## What shipped
- `app/risk/news_event_filter.py` — pure, calendar-driven engine.
- API: `GET /api/public/news-risk`.
- 19 unit tests in `tests/test_news_event_filter.py`.
- Config block + `.env.example`.

## Engine surface
- `MarketEvent(name, event_time, severity, symbols, kind)` — tz-aware; `symbols`
  empty ⇒ macro (affects all), otherwise scoped (and normalised, `ARB`↔`ARBUSDT`).
- `NewsEventCalendar` — in-memory registry the operator populates from a **real**
  feed / admin input (no fake data; empty calendar ⇒ allow everything).
- `can_open_entry(symbol)` → `NewsRiskDecision`.
- `news_risk_snapshot()` — payload for the public API.

## Windows
- Block from `PRE_EVENT_BLOCK_MINUTES` before to `POST_EVENT_BLOCK_MINUTES`
  after a high-impact event.
- High-impact = `HIGH` severity or a name in `HIGH_IMPACT_EVENTS`
  (CPI, FOMC, NFP, FED). Lower-impact events use half the window.

## Allowed during event windows
Monitoring, analytics and paper mode are **never** blocked — only new
live/auto entries are gated. Existing positions are never touched.

## Diagnostics
`news_filter_enabled`, `news_allowed`, `news_block_reason`, `blocking_event`,
`minutes_to_event`, `event_severity`.

## Safety / compatibility
Flag off → always allows (but the snapshot still lists upcoming events for the
UI). Pure, no I/O, no order interaction.

## Validation
`compileall` ✓ · `ruff` ✓ · `black` ✓ · `pytest tests/test_news_event_filter.py`
→ 19 passed.
