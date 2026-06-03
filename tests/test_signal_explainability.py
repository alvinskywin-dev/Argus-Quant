"""Sprint 22B — Signal Explainability Engine (pure, descriptive)."""

from __future__ import annotations

import pytest

from app.analytics.signal_explainability import (
    SignalReasoning,
    explain_signal,
    explain_signal_dict,
    render_telegram,
)


def _long_sig(**over):
    base = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "confidence": 88,
        "trend_score": 25,
        "structure_score": 18,
        "setup_score": 12,
        "entry_score": 3,
        "regime_score": 5,
        "market_regime": "LOW_VOLATILITY",
        "entry_low": 100,
        "entry_high": 101,
        "stop_loss": 95,
        "tp1": 110,
        "risk_reward": 1.8,
        "rr_method": "PREV_1D_SUPPORT",
    }
    base.update(over)
    return base


def test_long_reasoning_basic():
    r = explain_signal(_long_sig())
    assert r.direction == "LONG"
    assert r.why_long
    assert r.why_not_short
    assert not r.why_short


def test_short_reasoning_basic():
    r = explain_signal(_long_sig(side="SHORT"))
    assert r.direction == "SHORT"
    assert r.why_short
    assert r.why_not_long
    assert not r.why_long


def test_confidence_explanation_has_signed_factors():
    r = explain_signal(_long_sig())
    text = " ".join(r.confidence_explanation)
    assert "trend" in text.lower()
    assert any(x.startswith("+") for x in r.confidence_explanation)


def test_negative_factor_shown():
    r = explain_signal(_long_sig(regime_score=-5))
    assert any("-5" in x for x in r.confidence_explanation)


def test_sl_explanation_mentions_distance():
    r = explain_signal(_long_sig())
    assert "%" in r.sl_explanation
    assert "1D" in r.sl_explanation or "support" in r.sl_explanation.lower()


def test_tp_explanation_mentions_rr():
    r = explain_signal(_long_sig())
    assert "1.8" in r.tp_explanation or "RR" in r.tp_explanation


def test_regime_impact_low_vol():
    r = explain_signal(_long_sig(market_regime="LOW_VOLATILITY"))
    assert "LOW_VOLATILITY" in r.market_regime_impact
    assert "relax" in r.market_regime_impact.lower()


def test_regime_impact_high_vol():
    r = explain_signal(_long_sig(market_regime="HIGH_VOLATILITY"))
    assert "tighten" in r.market_regime_impact.lower()


def test_empty_signal_degrades_gracefully():
    r = explain_signal({})
    assert r.direction == "LONG"  # default
    assert r.why_long  # has a fallback reason
    assert isinstance(r.confidence_explanation, list)


def test_missing_scores_no_crash():
    r = explain_signal({"symbol": "X", "side": "SHORT"})
    assert r.direction == "SHORT"
    assert r.why_short


def test_to_dict_shape():
    d = explain_signal_dict(_long_sig())
    assert "signal_reasoning" in d
    sr = d["signal_reasoning"]
    for key in (
        "direction",
        "why_long",
        "why_not_short",
        "confidence_explanation",
        "sl_explanation",
        "tp_explanation",
        "market_regime_impact",
    ):
        assert key in sr


def test_reasons_field_appended():
    r = explain_signal(_long_sig(reasons="BTC dominance stable;Funding neutral"))
    joined = " ".join(r.why_long)
    assert "dominance" in joined.lower() or "funding" in joined.lower()


def test_render_telegram_contains_header():
    txt = render_telegram(explain_signal(_long_sig()))
    assert "Why this trade" in txt
    assert "Why LONG" in txt
    assert "Why not SHORT" in txt


def test_render_telegram_short():
    txt = render_telegram(explain_signal(_long_sig(side="SHORT")))
    assert "Why SHORT" in txt
    assert "Why not LONG" in txt


def test_entry_mid_used_for_distance():
    r = explain_signal(_long_sig(entry_low=100, entry_high=100, stop_loss=90))
    # 10% distance
    assert "10.00%" in r.sl_explanation


def test_dataclass_type():
    assert isinstance(explain_signal(_long_sig()), SignalReasoning)
