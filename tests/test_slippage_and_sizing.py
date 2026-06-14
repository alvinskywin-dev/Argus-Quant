"""
Slippage guard (#4) + risk-based sizing (#5).

Pure-maths tests for the live-execution guards: the slippage sign convention /
threshold, and that risk-based notional sizes by the entry→stop distance and is
capped by margin availability.
"""

from __future__ import annotations

import pytest

from app.paper_engine.math import position_quantity, risk_based_notional
from app.risk.slippage import exceeds_slippage, slippage_bps


# ── #4 slippage sign convention ───────────────────────────────────────────────
def test_slippage_long_higher_price_is_adverse():
    # LONG paying 0.5% more than intended -> +50 bps adverse.
    assert slippage_bps("LONG", 100.0, 100.5) == pytest.approx(50.0)
    assert slippage_bps("BUY", 100.0, 100.5) == pytest.approx(50.0)


def test_slippage_long_lower_price_is_favorable():
    # LONG filling cheaper than intended is favourable -> negative bps.
    assert slippage_bps("LONG", 100.0, 99.5) == pytest.approx(-50.0)


def test_slippage_short_lower_price_is_adverse():
    # SHORT receiving 0.5% less than intended -> +50 bps adverse.
    assert slippage_bps("SHORT", 100.0, 99.5) == pytest.approx(50.0)
    assert slippage_bps("SELL", 100.0, 99.5) == pytest.approx(50.0)


def test_slippage_zero_or_bad_reference():
    assert slippage_bps("LONG", 0.0, 100.0) == 0.0
    assert slippage_bps("LONG", None, 100.0) == 0.0


# ── #4 threshold ──────────────────────────────────────────────────────────────
def test_exceeds_slippage_threshold():
    # 60 bps adverse > 50 bps band.
    assert exceeds_slippage("LONG", 100.0, 100.6, 50.0) is True
    # 40 bps adverse < 50 bps band.
    assert exceeds_slippage("LONG", 100.0, 100.4, 50.0) is False
    # Favourable move never trips.
    assert exceeds_slippage("LONG", 100.0, 99.0, 50.0) is False


def test_exceeds_slippage_disabled_when_max_zero():
    assert exceeds_slippage("LONG", 100.0, 200.0, 0.0) is False


# ── #5 risk-based sizing ──────────────────────────────────────────────────────
def test_risk_based_notional_sizes_by_stop_distance():
    # Risk 1% of 10_000 = 100 USDT over a 2% stop distance -> 5_000 notional.
    bal, risk_pct, entry, stop = 10_000.0, 1.0, 100.0, 98.0
    notional = risk_based_notional(bal, risk_pct, entry, stop, leverage=10)
    assert notional == pytest.approx(5_000.0)
    # And the base-asset quantity follows from notional / entry.
    assert position_quantity(notional, entry) == pytest.approx(50.0)


def test_risk_based_notional_capped_by_margin():
    # Tiny stop distance would imply a huge notional; the margin cap binds.
    # margin cap = balance * frac (0.5) -> max margin 5_000; at 5x -> 25_000 notional.
    bal, risk_pct, entry, stop = 10_000.0, 1.0, 100.0, 99.99
    notional = risk_based_notional(bal, risk_pct, entry, stop, leverage=5, max_notional_frac=0.5)
    assert notional == pytest.approx(25_000.0)


def test_risk_based_notional_guards_bad_inputs():
    assert risk_based_notional(0.0, 1.0, 100.0, 98.0, leverage=5) == 0.0
    assert risk_based_notional(10_000.0, 1.0, 0.0, 98.0, leverage=5) == 0.0
