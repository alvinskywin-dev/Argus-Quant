"""
RR target selection (audit fix).

build_levels evaluates several TP2 candidates (atr / structure / liquidity) and
must pick the NEAREST target whose RR still meets the floor — not the farthest.
Maximising RR pushed TP2 out to distant swing/liquidity levels, inflating the
displayed ratio while lowering the probability the target is ever reached.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.indicators.smc import StructureSignal
from app.risk.levels import build_levels
from app.strategies.features import FeatureSnapshot


@pytest.fixture
def legacy_sl():
    """Force the simple LEGACY_ATR stop path + gate off, then restore."""
    saved = {
        "stoploss_engine_mode": settings.stoploss_engine_mode,
        "regime_adaptive_gate_enabled": settings.regime_adaptive_gate_enabled,
        "enable_liquidity_engine": getattr(settings, "enable_liquidity_engine", False),
        "min_rr": settings.min_rr,
    }
    settings.stoploss_engine_mode = "LEGACY_ATR"
    settings.regime_adaptive_gate_enabled = False
    settings.enable_liquidity_engine = False
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _snap(price: float, recent_high: float, recent_low: float, atr_pct: float = 1.0):
    return FeatureSnapshot(
        symbol="TESTUSDT",
        timeframe="15m",
        last_close=price,
        last_high=price,
        last_low=price,
        ema_fast=price,
        ema_slow=price,
        ema_50=price,
        ema_200=price,
        trend_up=True,
        trend_strength_adx=25.0,
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
        atr_pct=atr_pct,
        bb_width=1.0,
        bb_pos=0.5,
        vol_spike_pct=0.0,
        vwap_value=price,
        above_vwap=True,
        structure=StructureSignal(),
        range_pct_24bars=5.0,
        overextended_long=False,
        overextended_short=False,
        recent_high=recent_high,
        recent_low=recent_low,
    )


def test_long_prefers_nearest_valid_target(legacy_sl):
    # atr stop: price - 1.8*atr → risk = 1.8. atr TP2 multiplier ~2.5 (low vol) →
    # atr RR = 2.5. A far structure high (recent_high=120) yields a much larger
    # RR but a more distant target. With min_rr=2.0, both are valid; the nearest
    # (atr) must win.
    settings.min_rr = 2.0
    lv = build_levels(_snap(100.0, recent_high=120.0, recent_low=98.0), "LONG")
    assert lv is not None
    assert lv.rr_method == "atr"
    # The structure RR would have been (120-100)/1.8 ≈ 11.1; nearest keeps it modest.
    assert lv.risk_reward < 5.0


def test_long_falls_back_to_structure_when_atr_below_floor(legacy_sl):
    # Force a high min_rr so the atr candidate (RR≈2.5) is filtered out and only
    # the far structure target qualifies — selection should then use structure.
    settings.min_rr = 5.0
    lv = build_levels(_snap(100.0, recent_high=120.0, recent_low=98.0), "LONG")
    assert lv is not None
    assert lv.rr_method == "structure"
    assert lv.risk_reward >= 5.0


def test_ladder_stays_monotonic_after_nearest_selection(legacy_sl):
    settings.min_rr = 2.0
    lv = build_levels(_snap(100.0, recent_high=120.0, recent_low=98.0), "LONG")
    assert lv is not None
    assert lv.tp1 < lv.tp2 < lv.tp3, (lv.tp1, lv.tp2, lv.tp3)
