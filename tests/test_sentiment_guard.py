"""
Market sentiment guard determinism (audit fix).

The guard read the live major-price cache inside passes_market_filters, so the
same setup scored differently depending on WS state — non-reproducible. The bias
is now injectable (`sentiment_bias`) for deterministic tests/backtests, and the
whole guard is toggled by settings.market_sentiment_guard_enabled.
"""

from __future__ import annotations

import pytest

from app.ai_scoring import MTFDecision
from app.config import settings
from app.indicators.smc import StructureSignal
from app.risk.filters import passes_market_filters
from app.strategies.features import FeatureSnapshot


@pytest.fixture
def guard_on():
    saved = settings.market_sentiment_guard_enabled
    settings.market_sentiment_guard_enabled = True
    yield settings
    settings.market_sentiment_guard_enabled = saved


def _snap(adx: float = 30.0) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="TESTUSDT",
        timeframe="15m",
        last_close=100.0,
        last_high=100.0,
        last_low=100.0,
        ema_fast=100.0,
        ema_slow=100.0,
        ema_50=100.0,
        ema_200=100.0,
        trend_up=True,
        trend_strength_adx=adx,
        supertrend_dir=1,
        macd_hist=0.0,
        macd_cross_up=False,
        macd_cross_down=False,
        rsi_value=50.0,
        stoch_k=50.0,
        stoch_d=50.0,
        momentum_bull=True,
        momentum_bear=False,
        atr_value=1.0,
        atr_pct=1.0,
        bb_width=1.0,
        bb_pos=0.5,
        vol_spike_pct=0.0,
        vwap_value=100.0,
        above_vwap=True,
        structure=StructureSignal(),
        range_pct_24bars=5.0,
        overextended_long=False,
        overextended_short=False,
        recent_high=101.0,
        recent_low=99.0,
    )


def _decision(side: str = "LONG", confidence: float = 90.0) -> MTFDecision:
    return MTFDecision(
        side=side,
        confidence=confidence,
        tier="VIP",
        risk_level="MEDIUM",
        primary_tf="15m",
        contributing_tfs=["15m"],
        reasons=[],
        fake_breakout_prob=0.0,
    )


def test_injected_risk_off_penalises_long(guard_on):
    d = _decision("LONG", 90.0)
    ok, _ = passes_market_filters(_snap(), d, min_confidence=0.0, sentiment_bias="RISK_OFF")
    assert ok is True
    assert d.confidence == pytest.approx(83.0)  # -7


def test_injected_risk_on_penalises_short(guard_on):
    d = _decision("SHORT", 90.0)
    ok, _ = passes_market_filters(_snap(), d, min_confidence=0.0, sentiment_bias="RISK_ON")
    assert d.confidence == pytest.approx(85.0)  # -5


def test_neutral_only_penalises_weak_trend(guard_on):
    # NEUTRAL bias penalises only when ADX < 25. adx=22 avoids the weak-trend
    # penalty (which needs adx < 20), isolating the -4 neutral adjustment.
    weak = _decision("LONG", 90.0)
    passes_market_filters(_snap(adx=22.0), weak, min_confidence=0.0, sentiment_bias="NEUTRAL")
    assert weak.confidence == pytest.approx(86.0)  # -4 neutral

    strong = _decision("LONG", 90.0)
    passes_market_filters(_snap(adx=30.0), strong, min_confidence=0.0, sentiment_bias="NEUTRAL")
    assert strong.confidence == pytest.approx(90.0)  # adx >= 25 → no neutral penalty


def test_guard_disabled_is_deterministic(guard_on):
    settings.market_sentiment_guard_enabled = False
    d = _decision("LONG", 90.0)
    ok, _ = passes_market_filters(_snap(), d, min_confidence=0.0, sentiment_bias="RISK_OFF")
    assert d.confidence == pytest.approx(90.0)  # guard off → no adjustment
