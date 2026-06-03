# Sprint 22B — Signal Explainability Engine

## Goal
Every signal explains *why* it exists — direction, confidence breakdown, SL/TP
rationale, regime and liquidity context.

## What shipped
- `app/analytics/signal_explainability.py` — pure, descriptive engine.
- 16 unit tests in `tests/test_signal_explainability.py`.
- `SIGNAL_EXPLAINABILITY_ENABLED` flag (gates automatic attachment in the
  pipeline; the function is always safe to call on demand).

## Engine surface
- `explain_signal(sig) -> SignalReasoning` with: `why_long` / `why_short`,
  `why_not_short` / `why_not_long`, `confidence_explanation` (signed per-factor
  contributions), `sl_explanation`, `tp_explanation`, `market_regime_impact`,
  `liquidity_context`, `trend_structure_context`.
- `explain_signal_dict(sig)` → `{"signal_reasoning": {...}}`.
- `render_telegram(reasoning)` → compact collapsible "🔍 Why this trade?" block
  for the dashboard / admin / Telegram signal.

## Example
```json
{
  "signal_reasoning": {
    "direction": "LONG",
    "why_long": ["Higher-timeframe trend bullish (trend score 25)",
                 "4H/1H market structure intact (bullish)",
                 "15m entry trigger confirmed (3 factors)"],
    "why_not_short": ["Higher timeframe trend is bullish, not bearish",
                      "No bearish structure break"],
    "confidence_explanation": ["+25 HTF trend", "+18 market structure",
                                "+12 setup quality", "-3 market regime"],
    "sl_explanation": "Stop placed at previous 1D support/resistance with an ATR buffer — 5.00% from entry",
    "tp_explanation": "First target at a liquidity magnet giving RR 1.80",
    "market_regime_impact": "LOW_VOLATILITY: adaptive gate relaxed RR / SL-distance rules"
  }
}
```

## UI
`render_telegram()` produces a ready-to-embed "Why this trade?" section for the
dashboard, admin and Telegram signal. It is purely additive markup.

## Safety / compatibility
- Derives nothing new and **cannot change a signal** — read-only over fields the
  signal already carries.
- Missing fields degrade gracefully (a reason is omitted, never fabricated — no
  fake data). Empty signal still returns a well-formed object.

## Validation
`compileall` ✓ · `ruff` ✓ · `black` ✓ · `pytest tests/test_signal_explainability.py`
→ 16 passed.
