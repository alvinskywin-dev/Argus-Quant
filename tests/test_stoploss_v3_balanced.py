"""
Stop-Loss Engine V3 — Balanced ATR/structure mode (pure, no DB / network).

Covers candidate selection, the min/max distance safety rules, the structure→ATR
fallback, mode resolution (BALANCED / PREV_1D_SUPPORT / LEGACY_ATR all reachable),
regime-adaptive max-distance, and the Phase-4 diagnostics schema.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.risk.levels import compute_prev_1d_stop
from app.risk.stoploss_modes import (
    MODE_BALANCED,
    MODE_LEGACY_ATR,
    MODE_PREV_1D_SUPPORT,
    balanced_max_distance_percent,
    compute_balanced_stop,
    resolve_stoploss_mode,
)

# Balanced defaults pinned for deterministic tests.
ATR_MULT = 2.2
STRUCT_BUF = 0.25
MIN_PCT = 1.8
MAX_PCT = 8.0


@pytest.fixture
def cfg():
    """Pin the balanced + mode settings, then restore them."""
    keys = [
        "stoploss_engine_mode",
        "stoploss_engine_v2_enabled",
        "stoploss_method",
        "balanced_stop_atr_mult",
        "balanced_stop_structure_buffer_atr_mult",
        "balanced_stop_min_distance_percent",
        "balanced_stop_max_distance_percent",
        "balanced_stop_allow_1d_fallback",
        "regime_adaptive_gate_enabled",
        "low_vol_balanced_max_distance_percent",
        "high_vol_balanced_max_distance_percent",
        "sideways_balanced_max_distance_percent",
    ]
    saved = {k: getattr(settings, k) for k in keys}
    settings.stoploss_engine_mode = "BALANCED"
    settings.stoploss_engine_v2_enabled = False
    settings.stoploss_method = "PREV_1D_SUPPORT"
    settings.balanced_stop_atr_mult = ATR_MULT
    settings.balanced_stop_structure_buffer_atr_mult = STRUCT_BUF
    settings.balanced_stop_min_distance_percent = MIN_PCT
    settings.balanced_stop_max_distance_percent = MAX_PCT
    settings.balanced_stop_allow_1d_fallback = False
    settings.regime_adaptive_gate_enabled = False
    settings.low_vol_balanced_max_distance_percent = 12.0
    settings.high_vol_balanced_max_distance_percent = 6.0
    settings.sideways_balanced_max_distance_percent = 6.0
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _bal(side, entry, atr, low, high, *, max_pct=MAX_PCT, min_pct=MIN_PCT):
    return compute_balanced_stop(
        side,
        entry,
        atr,
        low,
        high,
        atr_mult=ATR_MULT,
        structure_buffer_atr_mult=STRUCT_BUF,
        min_pct=min_pct,
        max_pct=max_pct,
    )


# 1. LONG ATR stop valid — ATR stop is the more conservative (lower) candidate.
def test_long_atr_stop_valid(cfg):
    # atr_stop = 100 - 2.2*2 = 95.6 (4.4%); struct = 98 - 0.5 = 97.5 (2.5%)
    d = _bal("LONG", 100.0, 2.0, low=98.0, high=110.0)
    assert d["sl_valid"] is True
    assert d["selected_stop_source"] == "ATR"
    assert d["stop_loss"] == pytest.approx(95.6)
    assert d["sl_distance_percent"] == pytest.approx(4.4)


# 2. SHORT ATR stop valid.
def test_short_atr_stop_valid(cfg):
    # atr_stop = 104.4 (4.4%); struct = 102 + 0.5 = 102.5 (2.5%)
    d = _bal("SHORT", 100.0, 2.0, low=90.0, high=102.0)
    assert d["sl_valid"] is True
    assert d["selected_stop_source"] == "ATR"
    assert d["stop_loss"] == pytest.approx(104.4)
    assert d["sl_distance_percent"] == pytest.approx(4.4)


# 3. LONG structure stop valid — structure lower than ATR but within max.
def test_long_structure_stop_valid(cfg):
    # atr_stop = 95.6 (4.4%); struct = 94.5 - 0.5 = 94.0 (6.0%) -> structure wins
    d = _bal("LONG", 100.0, 2.0, low=94.5, high=110.0)
    assert d["sl_valid"] is True
    assert d["selected_stop_source"] == "STRUCTURE"
    assert d["stop_loss"] == pytest.approx(94.0)
    assert d["sl_distance_percent"] == pytest.approx(6.0)


# 4. SHORT structure stop valid.
def test_short_structure_stop_valid(cfg):
    # atr_stop = 104.4 (4.4%); struct = 105.5 + 0.5 = 106.0 (6.0%) -> structure wins
    d = _bal("SHORT", 100.0, 2.0, low=90.0, high=105.5)
    assert d["sl_valid"] is True
    assert d["selected_stop_source"] == "STRUCTURE"
    assert d["stop_loss"] == pytest.approx(106.0)
    assert d["sl_distance_percent"] == pytest.approx(6.0)


# 5. Structure too far -> falls back to ATR.
def test_structure_too_far_falls_back_to_atr(cfg):
    # struct = 88 - 0.5 = 87.5 (12.5% > 8) -> fallback ATR 95.6 (4.4%)
    d = _bal("LONG", 100.0, 2.0, low=88.0, high=110.0)
    assert d["sl_valid"] is True
    assert d["selected_stop_source"] == "ATR"
    assert d["stop_loss"] == pytest.approx(95.6)


# 6. ATR too close -> widen to minimum distance.
def test_atr_too_close_widens_to_min(cfg):
    # atr_stop = 100 - 2.2*0.5 = 98.9 (1.1% < 1.8) -> widen to 98.2 (1.8%)
    d = _bal("LONG", 100.0, 0.5, low=99.5, high=110.0)
    assert d["sl_valid"] is True
    assert d["sl_widened"] is True
    assert d["sl_distance_percent"] == pytest.approx(MIN_PCT)
    assert d["stop_loss"] == pytest.approx(98.2)


# 7. Too far even after ATR fallback -> reject.
def test_too_far_after_fallback_rejects(cfg):
    # atr_stop = 100 - 2.2*5 = 89 (11% > 8); struct far too -> reject sl_too_far
    d = _bal("LONG", 100.0, 5.0, low=85.0, high=110.0)
    assert d["sl_valid"] is False
    assert d["sl_reject_reason"] == "sl_too_far"


# 8. PREV_1D_SUPPORT mode still works (resolution + pure stop).
def test_prev_1d_support_mode_still_works(cfg):
    settings.stoploss_engine_mode = "PREV_1D_SUPPORT"
    assert resolve_stoploss_mode() == MODE_PREV_1D_SUPPORT
    d = compute_prev_1d_stop(
        "LONG",
        100.0,
        95.0,
        110.0,
        5.0,
        buffer_mult=0.2,
        min_pct=2.0,
        max_pct=10.0,
    )
    assert d["sl_valid"] is True
    assert d["stop_loss"] == pytest.approx(94.0)


# 9. LEGACY_ATR mode still works (explicit + legacy V2-flag fallback).
def test_legacy_atr_mode_still_works(cfg):
    settings.stoploss_engine_mode = "LEGACY_ATR"
    assert resolve_stoploss_mode() == MODE_LEGACY_ATR
    # Empty mode + V2 disabled also resolves to LEGACY_ATR.
    settings.stoploss_engine_mode = ""
    assert resolve_stoploss_mode() == MODE_LEGACY_ATR
    # Empty mode + V2 enabled resolves to PREV_1D_SUPPORT (back-compat).
    settings.stoploss_engine_v2_enabled = True
    assert resolve_stoploss_mode() == MODE_PREV_1D_SUPPORT


# 10. Regime LOW_VOLATILITY increases max distance.
def test_regime_low_vol_increases_max_distance(cfg):
    settings.regime_adaptive_gate_enabled = True
    assert balanced_max_distance_percent("LOW_VOLATILITY") == 12.0
    assert balanced_max_distance_percent("LOW_VOLATILITY") > MAX_PCT


# 11. Regime HIGH_VOLATILITY decreases max distance.
def test_regime_high_vol_decreases_max_distance(cfg):
    settings.regime_adaptive_gate_enabled = True
    assert balanced_max_distance_percent("HIGH_VOLATILITY") == 6.0
    assert balanced_max_distance_percent("SIDEWAYS") == 6.0
    assert balanced_max_distance_percent("HIGH_VOLATILITY") < MAX_PCT
    # Disabled gate -> base value regardless of regime.
    settings.regime_adaptive_gate_enabled = False
    assert balanced_max_distance_percent("LOW_VOLATILITY") == MAX_PCT


# 12. Diagnostics contain selected_stop_source (and the full Phase-4 schema).
def test_diagnostics_contain_selected_stop_source(cfg):
    d = _bal("LONG", 100.0, 2.0, low=98.0, high=110.0)
    for key in (
        "stoploss_engine_mode",
        "stoploss_method",
        "balanced_atr_stop",
        "balanced_structure_stop",
        "selected_stop_source",
        "sl_distance_percent",
        "sl_min_distance_percent",
        "sl_max_distance_percent",
        "sl_reject_reason",
    ):
        assert key in d
    assert d["stoploss_engine_mode"] == MODE_BALANCED
    assert d["stoploss_method"] == "ATR_STRUCTURE_BALANCED"
    assert d["selected_stop_source"] in ("ATR", "STRUCTURE")
