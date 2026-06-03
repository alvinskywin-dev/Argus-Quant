"""Regime Adaptive Gate V1 — threshold adaptation + hard clamps (pure, no DB)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.risk.regime_adaptive_gate import (
    HARD_MAX_SL_DISTANCE_PERCENT,
    HARD_MIN_CONFIDENCE,
    HARD_MIN_RR,
    get_effective_thresholds,
)

BASE_RR = 1.5
BASE_SL = 10.0
BASE_CONF = 80.0


@pytest.fixture
def gate():
    """Save/restore the adaptive-gate settings around a test."""
    keys = [
        k
        for k in vars(settings).keys()
        if k.startswith(
            ("regime_adaptive", "low_vol", "high_vol", "bull", "bear", "sideways", "normal")
        )
    ]
    saved = {k: getattr(settings, k) for k in keys}
    # known defaults
    settings.regime_adaptive_gate_enabled = True
    settings.normal_min_rr = 1.5
    settings.normal_max_sl_distance_percent = 10.0
    settings.normal_min_confidence_delta = 0
    settings.low_vol_min_rr = 1.0
    settings.low_vol_max_sl_distance_percent = 15.0
    settings.low_vol_min_confidence_delta = -3
    settings.high_vol_min_rr = 1.8
    settings.high_vol_max_sl_distance_percent = 8.0
    settings.high_vol_min_confidence_delta = 3
    settings.sideways_min_rr = 1.6
    settings.sideways_max_sl_distance_percent = 8.0
    settings.sideways_min_confidence_delta = 3
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _eff(regime):
    return get_effective_thresholds(BASE_RR, BASE_SL, BASE_CONF, regime)


def test_disabled_returns_base_unchanged(gate):
    gate.regime_adaptive_gate_enabled = False
    t = _eff("LOW_VOLATILITY")
    assert t.enabled is False
    assert t.effective_min_rr == BASE_RR
    assert t.effective_max_sl_distance_percent == BASE_SL
    assert t.effective_min_confidence == BASE_CONF
    assert t.confidence_delta == 0


def test_low_volatility_relaxes(gate):
    t = _eff("LOW_VOLATILITY")
    assert t.effective_min_rr == 1.0  # reduced from 1.5
    assert t.effective_max_sl_distance_percent == 15.0  # increased from 10
    assert t.effective_min_confidence == 77.0  # 80 + (-3)
    assert "LOW_VOLATILITY" in t.reason


def test_high_volatility_tightens(gate):
    t = _eff("HIGH_VOLATILITY")
    assert t.effective_min_rr == 1.8  # increased
    assert t.effective_max_sl_distance_percent == 8.0  # reduced
    assert t.effective_min_confidence == 83.0  # 80 + 3


def test_sideways_tightens(gate):
    t = _eff("SIDEWAYS")
    assert t.effective_min_rr == 1.6
    assert t.effective_max_sl_distance_percent == 8.0
    assert t.effective_min_confidence == 83.0


def test_hard_clamps(gate):
    # Push every per-regime value past its clamp.
    gate.low_vol_min_rr = 0.5  # below floor
    gate.low_vol_max_sl_distance_percent = 30.0  # above ceiling
    gate.low_vol_min_confidence_delta = -50  # 80-50=30, below floor
    t = _eff("LOW_VOLATILITY")
    assert t.effective_min_rr == HARD_MIN_RR == 1.0
    assert t.effective_max_sl_distance_percent == HARD_MAX_SL_DISTANCE_PERCENT == 20.0
    assert t.effective_min_confidence == HARD_MIN_CONFIDENCE == 70.0


def test_unknown_regime_uses_normal(gate):
    t = _eff("WHO_KNOWS")
    assert t.effective_min_rr == gate.normal_min_rr
    assert t.effective_max_sl_distance_percent == gate.normal_max_sl_distance_percent
    assert t.effective_min_confidence == BASE_CONF + gate.normal_min_confidence_delta


def test_none_regime_uses_normal(gate):
    t = _eff(None)
    assert t.effective_min_rr == gate.normal_min_rr
    assert t.market_regime == "UNKNOWN"


def test_diagnostics_contains_base_and_effective(gate):
    d = _eff("LOW_VOLATILITY").to_diagnostics()
    assert d["regime_adaptive_enabled"] is True
    assert d["market_regime"] == "LOW_VOLATILITY"
    assert d["base_min_rr"] == BASE_RR
    assert d["effective_min_rr"] == 1.0
    assert d["base_max_sl_distance_percent"] == BASE_SL
    assert d["effective_max_sl_distance_percent"] == 15.0
    assert d["base_min_confidence"] == BASE_CONF
    assert d["effective_min_confidence"] == 77.0
    assert "regime_threshold_reason" in d
