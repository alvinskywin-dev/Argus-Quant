# Regime Adaptive Gate — Market Radar UI Badge — Report

**Date:** 2026-06-03
**Scope:** Frontend / UI only. No trading, scanner, or risk logic changed.
**Status:** ✅ Implemented, page serves, JS syntax-checked, image builds.
**Tests:** 393 passing (unchanged — UI-only).

---

## 1. What shipped

A compact, theme-matched **Adaptive Gate** badge on `/market-radar`, placed
directly under the Market Bias / Risk / Sentiment cards. It reads the existing
endpoint **`GET /api/public/regime-adaptive-thresholds`** — no new backend.

Shown:
```
Adaptive Gate: ON · RELAXED        (pill)
Regime: LOW_VOLATILITY
RR: 1.5 → 1.0
SL Max: 10% → 15%
Confidence: 80 → 77
```

Behaviour:
- **Enabled:** shows `base → effective` for RR, SL-max and confidence, with the
  changed value coloured **green when relaxed** / **orange when stricter**. The
  pill reads `ON · RELAXED` (green) or `ON · STRICTER` (orange) based on the net
  direction of the three thresholds.
- **Disabled:** pill reads `OFF` (neutral gray); shows the **base thresholds
  only** (no arrows).
- **Endpoint fails:** the badge degrades gracefully — pill reads
  `unavailable` (gray) and the values blank; the structure is preserved so the
  next 45s refresh can recover.

## 2. Files changed

| File | Change |
|------|--------|
| `app/dashboard/server.py` → `_market_radar_page_html()` | Added badge CSS (`.mr-gate*`), the `#mr-gate` HTML block, and the `loadAdaptiveGate()` / `gateThresh()` JS wired into the existing 45s refresh loop |

No other files. No endpoint, model, scanner, or risk change.

## 3. Style

- Matches the ARGUS QUANT dark theme (same gradient card, border, and palette
  as the surrounding Market Radar cards).
- Colour semantics: green `#20ff80` (relaxed), orange `#ff9f43` (stricter),
  gray `#8aa0b8` (disabled/unavailable).
- **Mobile responsive:** the flex row stacks vertically under 600px (matches the
  page's existing breakpoints).

## 4. Validation

| Step | Result |
|------|--------|
| `python -m compileall app/dashboard/server.py` | ✅ clean |
| `node --check` on the extracted page JS | ✅ syntax OK |
| Page renders (`GET /market-radar` → 200, badge present) | ✅ |
| `pytest -q` (full) | ✅ 393 passed |
| `ruff check` / `black --check` (server.py) | ✅ clean |
| `docker compose build bot` | ✅ image built |

## 5. Notes

- CSP-safe: no new external resources; the inline page script is already
  permitted by the dashboard CSP (`script-src 'self' 'unsafe-inline'`).
- The badge reflects whatever the backend reports; with
  `REGIME_ADAPTIVE_GATE_ENABLED=false` it simply shows `OFF` + base values.

## Commit

`Add Market Radar badge for Regime Adaptive Gate`
