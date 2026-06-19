"""
15M entry trigger requirement (audit fix).

The strict MTF pipeline's entry layer scored 5 equal-weight factors and passed
on any ENTRY_PASS_SCORE of them. With the default of 2 a setup could clear the
layer on passive alignment alone (EMA pullback + VWAP reclaim) with no
price-action trigger. ENTRY_REQUIRE_TRIGGER (default on) now demands at least one
of BOS / FVG retest / OB retest.
"""

from __future__ import annotations

import pytest

from app.ai_scoring.mtf import evaluate_pipeline
from app.config import settings
from app.indicators.smc import StructureSignal
from app.strategies.features import FeatureSnapshot


@pytest.fixture
def entry_settings():
    saved = {
        "entry_pass_score": settings.entry_pass_score,
        "entry_require_trigger": settings.entry_require_trigger,
    }
    settings.entry_pass_score = 2
    settings.entry_require_trigger = True
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _snap(tf: str, *, ema_50=100.0, ema_200=100.0, structure=None, above_vwap=True, **ov):
    base = dict(
        symbol="TESTUSDT",
        timeframe=tf,
        last_close=111.0,
        last_high=111.0,
        last_low=111.0,
        ema_fast=110.0,
        ema_slow=100.0,
        ema_50=ema_50,
        ema_200=ema_200,
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
        vwap_value=100.0,
        above_vwap=above_vwap,
        structure=structure or StructureSignal(),
        range_pct_24bars=5.0,
        overextended_long=False,
        overextended_short=False,
        recent_high=120.0,
        recent_low=98.0,
    )
    base.update(ov)
    return FeatureSnapshot(**base)


def _snaps(m15_structure: StructureSignal):
    """Four LONG-biased snapshots that clear layers 1-3; 15M structure varies."""
    return {
        # 1D trend: EMA50 > EMA200, close > EMA200 → LONG
        "1d": _snap("1d", ema_50=110.0, ema_200=100.0),
        # 4H structure: 2 hits (BOS + OB)
        "4h": _snap("4h", structure=StructureSignal(bos_bull=True, ob_bull=True)),
        # 1H setup: pullback + retest + VWAP (+ EMA align) ≥ 3
        "1h": _snap("1h", structure=StructureSignal(pullback_bull=True, retest_bull=True)),
        # 15M entry: under test
        "15m": _snap("15m", structure=m15_structure),
    }


def test_alignment_only_rejected_when_trigger_required(entry_settings):
    # EMA pullback + VWAP reclaim = score 2 (meets ENTRY_PASS_SCORE) but no
    # BOS/FVG/OB trigger → rejected.
    decision, rejection = evaluate_pipeline(_snaps(StructureSignal()))
    assert decision is None
    assert rejection is not None and rejection.stage == "entry"
    assert "no-trigger" in rejection.detail


def test_alignment_only_passes_when_trigger_not_required(entry_settings):
    settings.entry_require_trigger = False
    decision, rejection = evaluate_pipeline(_snaps(StructureSignal()))
    assert rejection is None
    assert decision is not None and decision.side == "LONG"


def test_passes_with_a_trigger_present(entry_settings):
    # OB retest (trigger) + EMA pullback + VWAP → score 3 with a trigger.
    decision, rejection = evaluate_pipeline(_snaps(StructureSignal(ob_bull=True)))
    assert rejection is None
    assert decision is not None
    assert decision.entry_factors["OB retest"] is True
