# LANDING PAGE V5 — Conversion Fix

## What changed

- Reordered homepage to match the selected mockup and conversion funnel:
  1. Hero
  2. Live stats
  3. Trusted Partner Exchanges
  4. Telegram CTA
  5. Latest Live Signals
  6. Performance Summary
  7. Support / Donate
  8. FAQ
  9. Footer

- Partner exchange cards now always render even when affiliate URLs are not configured. If a URL is missing, the card shows a disabled setup prompt instead of disappearing.
- Donate section now always renders. If wallet addresses are missing, it shows visible placeholder cards instead of hiding the section.
- Added a stronger Support the Project layout with a short explanation card and wallet cards.
- Added Donate link to navbar.
- Added `#telegram-section` and `#support-section` anchors.
- Kept scanner, signal engine, backtest, database, Telegram bot, and Docker logic unchanged.

## Why

The previous page looked good technically but underperformed as a conversion page because exchange affiliate cards and donation widgets could disappear when environment variables were not set. This made the page look empty and reduced monetization opportunities.

## Validation

- `app/dashboard/server.py` passes Python compile validation.
- No trading logic was modified.
