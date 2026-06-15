"""
TP ladder ordering guard (audit fix #7).

A far structure/liquidity TP2 (or a low-RR TP2) could otherwise sit beyond the
fixed 3.5R TP3 — or below the 1.2R TP1 — letting the tracker tag a "higher"
level first and overstating RR. build_levels must always return a strictly
monotonic ladder.
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
    }
    settings.stoploss_engine_mode = "LEGACY_ATR"
    settings.regime_adaptive_gate_enabled = False
    settings.enable_liquidity_engine = False
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _snap(price: float, recent_high: float, recent_low: float) -> FeatureSnapshot:
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
        atr_pct=1.0,
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


def test_long_tp2_beyond_tp3_is_reordered(legacy_sl):
    # recent_high=130 forces a structure TP2 far past the 3.5R TP3.
    lv = build_levels(_snap(100.0, recent_high=130.0, recent_low=98.0), "LONG")
    assert lv is not None
    assert lv.tp1 < lv.tp2 < lv.tp3, (lv.tp1, lv.tp2, lv.tp3)


def test_short_tp2_beyond_tp3_is_reordered(legacy_sl):
    lv = build_levels(_snap(100.0, recent_high=102.0, recent_low=70.0), "SHORT")
    assert lv is not None
    assert lv.tp1 > lv.tp2 > lv.tp3, (lv.tp1, lv.tp2, lv.tp3)


def test_normal_atr_ladder_stays_ordered(legacy_sl):
    # No extreme structure target → ATR ladder, still monotonic.
    lv = build_levels(_snap(100.0, recent_high=101.0, recent_low=99.0), "LONG")
    assert lv is not None
    assert lv.tp1 < lv.tp2 < lv.tp3
