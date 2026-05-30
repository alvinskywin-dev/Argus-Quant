# Multi-Language System Report
**ALPHA RADAR SIGNALS — Global i18n V1**
Generated: 2026-05-30

---

## Overview

A lightweight, client-side multi-language system covering **22 languages** and **56 UI label keys** per locale. Translations are loaded lazily from static JSON files — only the selected language is fetched, keeping VPS memory usage minimal.

---

## Languages Supported (22)

| Code | Language        | Script    | Direction |
|------|-----------------|-----------|-----------|
| en   | English         | Latin     | LTR       |
| zh   | 中文 (Chinese)   | Han       | LTR       |
| hi   | हिन्दी (Hindi)   | Devanagari| LTR       |
| es   | Español         | Latin     | LTR       |
| pt   | Português       | Latin     | LTR       |
| ru   | Русский         | Cyrillic  | LTR       |
| vi   | Tiếng Việt      | Latin+    | LTR       |
| km   | ខ្មែរ (Khmer)    | Khmer     | LTR       |
| id   | Bahasa Indonesia| Latin     | LTR       |
| ja   | 日本語           | CJK       | LTR       |
| ko   | 한국어           | Hangul    | LTR       |
| tr   | Türkçe          | Latin     | LTR       |
| de   | Deutsch         | Latin     | LTR       |
| fr   | Français        | Latin     | LTR       |
| it   | Italiano        | Latin     | LTR       |
| ar   | العربية (Arabic) | Arabic    | **RTL**   |
| th   | ภาษาไทย (Thai)  | Thai      | LTR       |
| fil  | Filipino        | Latin     | LTR       |
| pl   | Polski          | Latin     | LTR       |
| uk   | Українська      | Cyrillic  | LTR       |
| bn   | বাংলা (Bengali) | Bengali   | LTR       |
| ur   | اردو (Urdu)     | Nastaliq  | **RTL**   |

---

## Architecture

### File Structure
```
app/
├── locales/
│   ├── en.json   ← base (fallback)
│   ├── zh.json
│   ├── hi.json
│   ├── es.json
│   ├── pt.json
│   ├── ru.json
│   ├── vi.json
│   ├── km.json
│   ├── id.json
│   ├── ja.json
│   ├── ko.json
│   ├── tr.json
│   ├── de.json
│   ├── fr.json
│   ├── it.json
│   ├── ar.json
│   ├── th.json
│   ├── fil.json
│   ├── pl.json
│   ├── uk.json
│   ├── bn.json
│   └── ur.json
└── dashboard/
    ├── i18n.py       ← server-side loader + cache
    └── server.py     ← API endpoints + HTML/JS i18n integration
```

### Server-Side (`app/dashboard/i18n.py`)
- `load_locale(lang)` — loads and LRU-caches the JSON file for the given language
- `translate(key, lang)` — returns translated string, falls back to English
- `is_rtl(lang)` — returns True for Arabic and Urdu
- `SUPPORTED_LANGUAGES` — list of `{code, name}` dicts used by the API

### API Endpoints
| Endpoint | Description |
|----------|-------------|
| `GET /api/public/languages` | Returns all 22 supported languages with code + native name |
| `GET /api/public/translations?lang=xx` | Returns the full translation JSON for a language (fallback: en) |

### Client-Side (`_PUBLIC_HTML` inline JS)
| Function | Purpose |
|----------|---------|
| `i18nInit()` | Entry point — detects language, loads translations, builds menu |
| `_detectLang()` | Checks `localStorage.ar_lang`, then `navigator.language` |
| `i18nLoad(lang)` | Lazy-fetches `/api/public/translations?lang=xx`, caches in memory |
| `i18nApply(dict, lang)` | Sets `textContent` on all `[data-i18n]` elements; toggles `dir=rtl` |
| `i18nSet(lang)` | Changes language, persists to `localStorage`, re-applies translations |
| `buildLangMenu()` | Populates the dropdown from `/api/public/languages` |
| `toggleLangMenu()` | Opens/closes the dropdown |

---

## Translation Keys (56 per locale)

| Key | English value |
|-----|--------------|
| nav.signals | Signals |
| nav.performance | Performance |
| nav.stats | Stats |
| nav.about | About |
| nav.faq | FAQ |
| nav.join_telegram | Join Telegram |
| hero.sub | Multi-Timeframe Analysis • Risk Managed • 24/7 Scanner |
| hero.accuracy | High Accuracy |
| hero.accuracy_sub | AI Validated |
| hero.risk | Risk Managed |
| hero.risk_sub | Smart Entries |
| hero.scanner | 24/7 Scanner |
| hero.scanner_sub | Never Miss Setup |
| hero.live_perf | Live Performance |
| hero.live_perf_sub | Transparent Stats |
| hero.btn_performance | View Performance |
| stats.total | Total Signals (30D) |
| stats.win_rate | Win Rate (30D) |
| stats.avg_rr | Avg RR (30D) |
| stats.markets | Markets Scanned |
| stats.positions | Open Positions |
| section.exchanges | Trusted Partner Exchanges |
| section.exchanges_sub | Trade on the best platforms with exclusive bonuses |
| section.strategy | LIVE STRATEGY ENGINE |
| section.strategy_sub | Exact strategy logic… |
| section.signals | Latest Live Signals |
| section.signals_all | View All Signals → |
| section.perf | Performance Summary |
| section.perf_sub | Transparent. Verified. Real results. |
| section.faq | Frequently Asked Questions |
| table.time | Time |
| table.symbol | Symbol |
| table.side | Side |
| table.tf | TF |
| table.confidence | Confidence |
| table.rr | RR |
| table.status | Status |
| table.pnl | PNL |
| signal.btn | Analysis |
| signal.title | Signal Analysis |
| signal.close | ✕ Close |
| signal.loading | Loading… |
| signal.no_data | Detailed diagnostics not available… |
| signal.trend | Trend Engine |
| signal.structure | Market Structure |
| signal.setup | Setup Engine |
| signal.entry | Entry Timing |
| signal.funding | Funding Filter |
| signal.risk | Risk Filter |
| footer.tagline | AI-Powered. Data-Driven. Trader-Focused. |
| footer.links | Links |
| footer.community | Community |
| footer.legal | Legal |
| footer.terms | Terms of Service |
| footer.privacy | Privacy Policy |
| footer.risk_disc | Risk Disclaimer |
| footer.copy | © 2026 ALPHA RADAR SIGNALS. All rights reserved. |
| lang.select | Language |

---

## Intentionally Untranslated (Trading Terms)

The following terms remain in English in all locales as they are universal trading terminology:

`LONG` · `SHORT` · `TP1` · `TP2` · `SL` · `RR` · `EMA` · `BOS` · `CHOCH` · `FVG` · `VWAP`

---

## RTL Support

Arabic (`ar`) and Urdu (`ur`) automatically set `dir="rtl"` and `lang` on the `<html>` element when selected. All other languages use LTR.

---

## Language Persistence & Detection

1. **Priority 1:** `localStorage.getItem('ar_lang')` — user's explicit selection
2. **Priority 2:** `navigator.language` — browser preference
3. **Fallback:** English (`en`)

---

## Memory & Performance

- Each locale file: ~4 KB minified JSON
- Server-side: `@lru_cache(maxsize=22)` — at most 22 dicts in memory (~88 KB total)
- Client-side: in-session JS object cache per loaded language
- English (default) requires **zero extra requests** on page load
- Non-English: **one** 4 KB JSON fetch, then cached for the session

---

## Components NOT Modified

As required, the following were not touched:

- `app/scanner/` — signal scanner
- `app/database/` — database models and migrations
- `app/telegram_bot/` — Telegram bot
- `app/ai_scoring/` — signal engine (MTF pipeline)

---

## Validation

```bash
# API endpoints
curl -s http://127.0.0.1:8010/api/public/languages    # 22 languages
curl -s "http://127.0.0.1:8010/api/public/translations?lang=zh"  # Chinese
curl -s "http://127.0.0.1:8010/api/public/translations?lang=ar"  # Arabic
curl -s "http://127.0.0.1:8010/api/public/translations?lang=xx"  # fallback → en

# Landing page
curl -s http://127.0.0.1:8010/ | grep -c 'data-i18n'  # 32 annotated elements
curl -s http://127.0.0.1:8010/ | grep -c 'i18nInit'   # JS engine present
```
