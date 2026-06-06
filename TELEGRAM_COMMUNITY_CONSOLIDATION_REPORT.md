# Telegram Community Consolidation Report

**Project:** ARGUS QUANT — Community Consolidation Phase
**Goal:** Temporarily merge all VIP / Elite VIP / Premium signal flows into the
single flagship public Telegram community **https://t.me/ArgusQuant** while the
engine is in optimization / adaptive-risk-tuning / live-validation.

This is a **feature-flag consolidation, not a deletion.** No tier code or env
variable was removed — multi-tier segmentation can be reactivated later by
flipping flags.

---

## 1. Routing changes

All signal routing is centralized through `_route_signal_chats()` in
`app/telegram_bot/bot.py`:

- When `TELEGRAM_SINGLE_PUBLIC_GROUP=true` **or** `TELEGRAM_COMMUNITY_MODE=true`
  (both default **true**), **every** signal — regardless of confidence/tier —
  routes to a single target:
  - `PUBLIC_TELEGRAM_CHAT_ID` →
    legacy `PUBLIC_CHAT_ID` →
    `TELEGRAM_SIGNAL_CHAT_ID` (fallback, so signals always land somewhere).
- VIP-only, Elite-only and Premium-only sends and **tier filtering are fully
  bypassed** in this mode. `_signal_tier()` is still computed for logging but no
  longer changes the destination.
- Comma-separated multi-channel targets are still supported.

New config (`app/config.py`, all env-backed, case-insensitive):

| Setting | Default | Purpose |
|---|---|---|
| `TELEGRAM_COMMUNITY_MODE` | `true` | Master consolidation switch |
| `TELEGRAM_SINGLE_PUBLIC_GROUP` | `true` | Route everything to the public group |
| `PUBLIC_TELEGRAM_CHAT_ID` | `""` | Flagship group chat id |
| `VIP_TELEGRAM_DISABLED` | `true` | Disable VIP sends |
| `ELITE_TELEGRAM_DISABLED` | `true` | Disable Elite sends |
| `PREMIUM_TELEGRAM_DISABLED` | `true` | Disable Premium sends |
| `VIP_ROUTING_ENABLED` | `false` | Future-ready tier switch |
| `ELITE_ROUTING_ENABLED` | `false` | Future-ready tier switch |
| `PREMIUM_ROUTING_ENABLED` | `false` | Future-ready tier switch |

**Backward compatibility:** the deprecated `VIP_CHAT_ID`, `ELITE_VIP_CHAT_ID` and
`PUBLIC_CHAT_ID` env vars and `settings` fields are **kept** (marked DEPRECATED in
code) and still readable. They are simply ignored while consolidation is active.

## 2. Disabled premium flows

Temporarily disabled (feature-flag OFF, **no code deleted**):

- VIP / Elite / Premium tier-specific Telegram routing.
- Tier filtering of the broadcast destination.
- Tier-specific caption titles (🔥 ELITE / 💎 VIP) — replaced by the unified
  community card while consolidation is active.

The legacy tier routing and captions remain in `bot.py` behind the
`_community_consolidation_active()` guard, ready to re-enable.

## 3. Public community strategy

**Phase 2 — Message restructure.** A new `format_community_signal()` in
`app/telegram_bot/formatter.py` renders every public signal in a premium +
educational format containing all required fields:

1. Signal (side + symbol) · 2. Confidence · 3. RR · 4. Market regime ·
5. Explainability ("Why this trade?") · 6. StopLoss mode (e.g.
`BALANCED ATR+STRUCTURE`) · 7. Partial-TP / break-even management line ·
8. Risk warning.

```
🧠 ARGUS QUANT SIGNAL

🟢 LONG — BTCUSDT
Confidence: 84%
RR: 1 : 2.1
Market Regime: LOW_VOLATILITY
StopLoss: BALANCED ATR+STRUCTURE

📊 Why this trade?
• 4H bullish structure intact
• Liquidity sweep below support
...

🎯 Entry / 🛑 Stop Loss / 💰 Targets
🔁 Management • Move SL to break-even after TP1
⚠️ Risk: Use proper risk management. Never overleverage.
```

The image-card path and the text-only fallback both use this format in community
mode.

**Phase 3 / 7 social-proof surfaces.** A new public endpoint
`GET /api/public/community-analytics` (`analytics_router.py`) aggregates from the
signal DB:

- signal frequency per day + average/day (7-day window)
- market-regime distribution
- headline public performance (closed, wins, win-rate)
- community URL + mode flags

Daily performance summaries, "Why no trade today?" transparency posts, and
shadow/live snapshots are intended to be scheduled broadcasts built on this
endpoint and existing performance/market-radar data (see Future work).

## 4. Future VIP reactivation plan

No code changes required — config only:

1. Set `TELEGRAM_SINGLE_PUBLIC_GROUP=false` and `TELEGRAM_COMMUNITY_MODE=false`.
2. Enable the desired tiers: `VIP_ROUTING_ENABLED=true` /
   `ELITE_ROUTING_ENABLED=true` (and clear the matching `*_TELEGRAM_DISABLED`).
3. Populate `VIP_CHAT_ID` / `ELITE_VIP_CHAT_ID` (still supported).
4. Restart the bot to load the new env.

This is gated to follow: stable live pilot → verified consistency → execution
maturity → stronger brand trust → multi-user live-beta readiness.

Future-ready architecture is preserved (not exposed now): VIP groups, Elite
execution, copy trading, multi-tier SaaS, low-latency feeds, auto-trading
subscriptions.

## 5. Analytics additions

- `GET /api/public/community-analytics` — signal frequency, regime distribution,
  public performance, community URL/mode.
- Telegram growth / engagement / click-through are returned as `null`
  placeholders pending the Telegram Analytics API integration.
- Dashboard branding/links now point at **https://t.me/ArgusQuant** via the
  `telegram_channel_url` default (drives nav button, hero CTA, and the big
  Telegram CTA section); the existing nav already links **Market Radar** and
  **Performance**.

## 6. Safety preserved

Consolidation changes **only the broadcast destination and message layout**. It
does **not** touch signal generation or risk:

- Confidence gate, RR filter, portfolio-exposure engine, news/event filter and
  StopLoss safety rules are **unchanged**.
- No RR/exposure/news bypass; no lowering of safety standards.
- No increase in signal frequency — the scanner emits exactly as before; only the
  routing target changed.
- The duplicate-active-signal publisher guard is unchanged.

## 7. Tests & validation

New: `tests/test_telegram_community_routing.py` (8 cases) — all route to the
public group, no VIP/Elite sends when disabled, legacy `PUBLIC_CHAT_ID` honored,
`TELEGRAM_SIGNAL_CHAT_ID` fallback, empty-target safety, env-flag-respecting
legacy routing (re-enable + disable-switch precedence), community message format
fields, and the dashboard link default.

```
python -m compileall app tests   # OK
pytest -q                        # 550 passed
ruff check .                     # All checks passed
black --check                    # clean
docker compose build bot         # Image built
```

The live bot was **not** restarted.

## Future work (documented, not built here)

- Scheduled daily performance / winrate / market-radar / "why no trade today"
  broadcasts to the public group (jobs on top of `community-analytics`).
- Telegram growth / engagement / CTR via the Telegram Analytics API.
- Optional extra hero buttons (Live Signals / dedicated "Join ARGUS QUANT") — the
  CTA URL is already wired; only HTML labels would change.
