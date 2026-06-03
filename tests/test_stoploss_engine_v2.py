"""
Stop-Loss Engine V2 — previous-1D support/resistance stop.

Tests the pure SL computation (`compute_prev_1d_stop`) and the prev-1D candle
extractor (`_extract_prev_1d`). No DB / network required.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from app.risk.levels import _extract_prev_1d, compute_prev_1d_stop

# Common guard config for the pure function.
CFG = dict(min_pct=2.0, max_pct=10.0, too_close_action="widen")


def _stop(side, entry, low, high, atr_1d, buffer_mult):
    return compute_prev_1d_stop(side, entry, low, high, atr_1d, buffer_mult=buffer_mult, **CFG)


# 1. LONG: entry=100, prev_1d_low=95, buffer=1 -> SL=94
def test_long_basic_stop_below_support():
    d = _stop("LONG", 100.0, 95.0, 110.0, atr_1d=5.0, buffer_mult=0.2)  # buffer = 1.0
    assert d["sl_valid"] is True
    assert d["sl_buffer"] == pytest.approx(1.0)
    assert d["stop_loss"] == pytest.approx(94.0)
    assert d["sl_distance_percent"] == pytest.approx(6.0)
    assert d["stoploss_method"] == "PREV_1D_SUPPORT"


# 2. SHORT: entry=100, prev_1d_high=105, buffer=1 -> SL=106
def test_short_basic_stop_above_resistance():
    d = _stop("SHORT", 100.0, 90.0, 105.0, atr_1d=5.0, buffer_mult=0.2)  # buffer = 1.0
    assert d["sl_valid"] is True
    assert d["sl_buffer"] == pytest.approx(1.0)
    assert d["stop_loss"] == pytest.approx(106.0)
    assert d["sl_distance_percent"] == pytest.approx(6.0)


# 3. LONG invalid: prev_1d_low above entry => reject
def test_long_support_above_entry_rejected():
    d = _stop("LONG", 100.0, 105.0, 120.0, atr_1d=5.0, buffer_mult=0.2)
    assert d["sl_valid"] is False
    assert d["sl_reject_reason"] == "support_not_below_entry"


# 4. SHORT invalid: prev_1d_high below entry => reject
def test_short_resistance_below_entry_rejected():
    d = _stop("SHORT", 100.0, 80.0, 95.0, atr_1d=5.0, buffer_mult=0.2)
    assert d["sl_valid"] is False
    assert d["sl_reject_reason"] == "resistance_not_above_entry"


# 5. Too far SL: distance > MAX_SL_DISTANCE_PERCENT => reject
def test_long_too_far_rejected():
    # prev_low=80, buffer=1 -> SL=79 -> dist 21% > 10%
    d = _stop("LONG", 100.0, 80.0, 120.0, atr_1d=5.0, buffer_mult=0.2)
    assert d["sl_valid"] is False
    assert d["sl_reject_reason"] == "sl_too_far"
    assert d["sl_distance_percent"] == pytest.approx(21.0)


# 6a. Too close SL: distance < MIN -> widen to the floor (default action)
def test_long_too_close_widens_to_floor():
    # prev_low=99.5, tiny buffer -> dist ~0.5% < 2% -> widen to 2% -> SL=98.0
    d = _stop("LONG", 100.0, 99.5, 110.0, atr_1d=1.0, buffer_mult=0.05)  # buffer 0.05
    assert d["sl_valid"] is True
    assert d.get("sl_widened") is True
    assert d["stop_loss"] == pytest.approx(98.0)
    assert d["sl_distance_percent"] == pytest.approx(2.0)


# 6b. Too close SL with reject action -> reject
def test_long_too_close_reject_action():
    d = compute_prev_1d_stop(
        "LONG",
        100.0,
        99.5,
        110.0,
        1.0,
        buffer_mult=0.05,
        min_pct=2.0,
        max_pct=10.0,
        too_close_action="reject",
    )
    assert d["sl_valid"] is False
    assert d["sl_reject_reason"] == "sl_too_close"


# 6c. SHORT too close widens above entry
def test_short_too_close_widens_to_floor():
    d = _stop("SHORT", 100.0, 90.0, 100.5, atr_1d=1.0, buffer_mult=0.05)
    assert d["sl_valid"] is True
    assert d.get("sl_widened") is True
    assert d["stop_loss"] == pytest.approx(102.0)
    assert d["sl_distance_percent"] == pytest.approx(2.0)


# buffer derives from ATR(1D): buffer = mult * atr_1d
def test_buffer_scales_with_atr():
    d = _stop("LONG", 100.0, 95.0, 110.0, atr_1d=8.0, buffer_mult=0.15)  # buffer = 1.2
    assert d["sl_buffer"] == pytest.approx(1.2)
    assert d["stop_loss"] == pytest.approx(93.8)


# _extract_prev_1d uses the previous completed candle (iloc[-2]), not the forming one
def test_extract_prev_1d_uses_second_to_last():
    n = 30
    df = pd.DataFrame(
        {
            "open_time": pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC"),
            "open": [100.0] * n,
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 - i for i in range(n)],
            "close": [100.0] * n,
        }
    )
    prev = _extract_prev_1d(df)
    assert prev is not None
    # iloc[-2] is index n-2
    assert prev["high"] == pytest.approx(101.0 + (n - 2))
    assert prev["low"] == pytest.approx(99.0 - (n - 2))
    assert prev["atr_1d"] >= 0.0


def test_extract_prev_1d_insufficient_data():
    df = pd.DataFrame({"open_time": [], "open": [], "high": [], "low": [], "close": []})
    assert _extract_prev_1d(df) is None
    assert _extract_prev_1d(None) is None


# ── V2 hardening: non-finite inputs must reject, never yield a NaN "valid" SL ──
# Regression for a silent bug: a NaN low/high/ATR slipped through every guard
# (NaN comparisons are all False) and produced stop_loss=NaN with sl_valid=True.


@pytest.mark.parametrize(
    "label,entry,low,high,atr",
    [
        ("nan_atr", 100.0, 95.0, 110.0, float("nan")),
        ("inf_atr", 100.0, 95.0, 110.0, float("inf")),
        ("nan_low", 100.0, float("nan"), 110.0, 5.0),
        ("nan_high", 100.0, 95.0, float("nan"), 5.0),
        ("nan_entry", float("nan"), 95.0, 110.0, 5.0),
        ("inf_entry", float("inf"), 95.0, 110.0, 5.0),
    ],
)
def test_non_finite_inputs_rejected(label, entry, low, high, atr):
    d = _stop(label and "LONG", entry, low, high, atr, buffer_mult=0.2)
    assert d["sl_valid"] is False
    assert d["sl_reject_reason"] == "non_finite_input"
    # Critically: never emit a NaN stop_loss flagged as usable.
    sl = d["stop_loss"]
    assert sl is None or math.isfinite(sl)


def test_finite_inputs_still_valid_after_guard():
    # The guard must not perturb the normal path.
    d = _stop("LONG", 100.0, 95.0, 110.0, atr_1d=5.0, buffer_mult=0.2)
    assert d["sl_valid"] is True
    assert d["stop_loss"] == pytest.approx(94.0)
