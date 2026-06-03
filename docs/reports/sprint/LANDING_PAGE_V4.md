# LANDING PAGE V4 — ALPHA RADAR SIGNALS

## Design Decisions

### Visual Language
- **Dark base** `#040d1a` — deep navy that reads as premium, not harsh black
- **Glassmorphism cards** — `backdrop-filter: blur(20px)` + semi-transparent backgrounds + subtle border glow on hover
- **Neon teal accent** `#00f5d4` — primary brand colour for CTAs, highlights, badges
- **Neon green** `#00ff7f` for positive PnL / LONG signals; **Neon red** `#ff3d5a` for SHORT / losses
- **Inter** typeface — clean, modern, legible at all weights
- All colours defined as CSS custom properties (`--teal`, `--green`, etc.) for consistency

### Animated Radar Hero
- Right-side hero: SVG-style CSS radar with 3 concentric rings, a rotating conic-gradient sweep (3 s linear infinite), 4 pulsing radar dots, and 3 floating price chips (BTC/ETH/SOL) that animate via keyframes
- On load, price chips update with live prices from `/api/public/prices`
- On mobile (`< 768 px`) the radar appears above the text for maximum visual impact

### Section Architecture (top → bottom)
| # | Section | Purpose |
|---|---------|---------|
| 1 | Hero | Hook — radar + headline + feature list + CTAs |
| 2 | Live Stats (5 cards) | Social proof — signals, win rate, RR, markets, open |
| 3 | Live Signals Table | Transparency — real-time trade feed |
| 4 | Performance | Credibility — equity curve + metrics |
| 5 | Partner Exchanges | Revenue — affiliate conversions |
| 6 | Telegram CTA | Primary conversion goal |
| 7 | Donations | Support goal |
| 8 | FAQ | Objection handling |
| 9 | Footer | Navigation + legal |

## Conversion Strategy

**Primary goal: Telegram Join**
- Hero has a primary `btn-primary` (gradient teal) Join Telegram button
- A large full-width Telegram CTA section mid-page (after performance data builds trust)
- A floating Telegram button fixed at bottom-right (always visible, never intrusive)
- Nav has a Telegram button for desktop users

**Secondary goal: Exchange Affiliate Registrations**
- 4 exchange cards placed after the signals/performance sections (trust is already established)
- Each card has a hover lift + glow effect to encourage clicks
- Click tracking via existing `/aff/{exchange}` redirect route

**Third goal: Donations**
- Placed after exchanges — final ask before FAQ
- Copy button + QR Code popup (via QRCode.js) lowers friction
- Toast notification confirms the copy action

**Funnel**: Visitor → Stats build trust → Signals show quality → Performance confirms results → Join Telegram → Register Exchange → Donate

## Mobile Strategy

- **Mobile-first responsive** via CSS media queries at 1020 px, 768 px, 480 px
- Stats bar: 5 cols → 3 cols → 2 cols → 1 col
- Hero: 2-col grid → single col (radar on top for visual hook)
- Exchange grid: 4 → 2 → 1 col
- Nav links hidden at `< 768 px` (mobile users use the floating TG button)
- Tables have `overflow-x: auto` with `-webkit-overflow-scrolling: touch`
- All touch targets >= 44 px

## Performance

- Google Fonts preconnected and loaded with `crossorigin` for CORS caching
- Chart.js loaded from jsDelivr CDN (widely cached)
- QRCode.js loaded from Cloudflare CDN (widely cached)
- All inline CSS / JS — no additional render-blocking requests
- Images: none — pure CSS/SVG icons
- JS API calls: 3 parallel fetches on load, then intervals (6 s stats, 30 s perf, 4 s prices)
- Equity curve chart rendered on canvas — GPU-accelerated

## ENV Configuration

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_CHANNEL_URL` | TG join link (hero, CTA, float button, footer) |
| `DISCORD_URL` | Discord link (nav, footer) |
| `BINANCE_AFFILIATE_URL` | Binance partner card |
| `BYBIT_AFFILIATE_URL` | Bybit partner card |
| `OKX_AFFILIATE_URL` | OKX partner card |
| `BITGET_AFFILIATE_URL` | Bitget partner card |
| `DONATE_USDT_TRC20` | USDT TRC20 wallet |
| `DONATE_USDT_BEP20` | USDT BEP20 wallet |
| `DONATE_BTC` | Bitcoin wallet |
| `DONATE_ETH` | Ethereum wallet |

Sections are conditionally rendered — if an ENV var is not set, its card/button is omitted gracefully.

## Admin Dashboard

Unchanged. All existing routes, APIs, and admin functionality preserved:
- `/admin` — admin dashboard (auth protected)
- `/api/public/*` — public data APIs
- `/aff/{exchange}` — affiliate click tracking
- `/signals`, `/performance`, `/stats`, `/paper`, `/backtest`, etc.
