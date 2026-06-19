"""
Confidence-floor gating (audit fix).

The scanner used to enforce the confidence floor inside passes_market_filters,
*before* the OI / funding / liquidity / regime adjustments were added. That made
the gate asymmetric: penalties could sink a setup but positive context could
never rescue one. The floor is now deferred to a single authoritative gate, so
passes_market_filters must support skipping the floor while still applying its
structural rejections and confidence adjustments.
"""

from __future__ import annotations

import pytest

from app.ai_scoring import MTFDecision
from app.indicators.smc import StructureSignal
from app.risk.filters import passes_market_filters
from app.strategies.features import FeatureSnapshot


def _snap(**overrides) -> FeatureSnapshot:
    base = dict(
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
        trend_strength_adx=25.0,  # no ADX bonus/penalty band
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
    base.update(overrides)
    return FeatureSnapshot(**base)


def _decision(confidence: float, side: str = "LONG") -> MTFDecision:
    return MTFDecision(
        side=side,
        confidence=confidence,
        tier="PUBLIC",
        risk_level="MEDIUM",
        primary_tf="15m",
        contributing_tfs=["15m"],
        reasons=[],
        fake_breakout_prob=0.0,
    )


def test_floor_enforced_by_default_rejects_low_confidence():
    decision = _decision(60.0)
    ok, reason = passes_market_filters(_snap(), decision, min_confidence=80.0)
    assert ok is False
    assert reason == "below_confidence_threshold"


def test_floor_skipped_lets_low_confidence_pass():
    decision = _decision(60.0)
    ok, reason = passes_market_filters(
        _snap(), decision, min_confidence=80.0, enforce_confidence_floor=False
    )
    assert ok is True
    assert reason is None
    # Confidence is unchanged here (neutral snapshot), so a later gate can still
    # add OI/funding/regime context before deciding.
    assert decision.confidence == 60.0


def test_structural_reject_still_fires_with_floor_skipped():
    # Dead volume is a structural rejection — independent of the confidence floor.
    decision = _decision(95.0)
    ok, reason = passes_market_filters(
        _snap(vol_spike_pct=-60.0),
        decision,
        min_confidence=80.0,
        enforce_confidence_floor=False,
    )
    assert ok is False
    assert reason == "low_volume"


def test_confidence_adjustments_apply_with_floor_skipped():
    # Weak trend (ADX < 20) applies a -6 penalty even when the floor is skipped,
    # so the mutated confidence is available to the deferred gate.
    decision = _decision(80.0)
    ok, _ = passes_market_filters(
        _snap(trend_strength_adx=15.0),
        decision,
        min_confidence=70.0,
        enforce_confidence_floor=False,
    )
    assert ok is True
    assert decision.confidence == pytest.approx(74.0)
