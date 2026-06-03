"""
Sprint 20B — unit tests for the paper futures maths (no DB required).
"""

from __future__ import annotations

import pytest

from app.paper_engine import math as pm


def test_quantity_and_margin():
    # 1000 USDT notional at price 50 -> 20 units; 10x lev -> 100 margin
    assert pm.position_quantity(1000, 50) == 20
    assert pm.required_margin(1000, 10) == 100
    assert pm.required_margin(1000, 0) == 1000  # guard: zero leverage


def test_liquidation_price_long_below_short_above_entry():
    entry = 100.0
    long_liq = pm.liquidation_price(pm.LONG, entry, 10)
    short_liq = pm.liquidation_price(pm.SHORT, entry, 10)
    assert long_liq < entry < short_liq
    # 10x => ~10% adverse move (minus mmr) wipes margin
    assert long_liq == pytest.approx(100 * (1 - 0.1 + 0.005))
    assert short_liq == pytest.approx(100 * (1 + 0.1 - 0.005))


def test_liquidation_price_never_negative():
    assert pm.liquidation_price(pm.LONG, 100, 1) >= 0.0


def test_unrealized_pnl_directional():
    # +10% move on 1000 notional
    assert pm.unrealized_pnl(pm.LONG, 100, 110, 1000) == pytest.approx(100)
    assert pm.unrealized_pnl(pm.SHORT, 100, 110, 1000) == pytest.approx(-100)
    assert pm.unrealized_pnl(pm.SHORT, 100, 90, 1000) == pytest.approx(100)


def test_roe_pct():
    # 100 USDT pnl on 100 USDT margin (10x, 1000 notional) = 100% ROE
    assert pm.roe_pct(100, 100) == pytest.approx(100)
    assert pm.roe_pct(50, 0) == 0.0


def test_price_move_pct_signed_by_side():
    assert pm.price_move_pct(pm.LONG, 100, 105) == pytest.approx(5)
    assert pm.price_move_pct(pm.SHORT, 100, 105) == pytest.approx(-5)


def test_funding_cost_long_pays_short_receives():
    # positive funding: long pays, short receives
    assert pm.funding_cost(pm.LONG, 1000, 0.0003, 1) == pytest.approx(0.3)
    assert pm.funding_cost(pm.SHORT, 1000, 0.0003, 1) == pytest.approx(-0.3)
    assert pm.funding_cost(pm.LONG, 1000, 0.0003, 0) == 0.0


def test_is_liquidated():
    long_liq = pm.liquidation_price(pm.LONG, 100, 10)
    assert pm.is_liquidated(pm.LONG, long_liq - 1, long_liq)
    assert not pm.is_liquidated(pm.LONG, long_liq + 1, long_liq)
    short_liq = pm.liquidation_price(pm.SHORT, 100, 10)
    assert pm.is_liquidated(pm.SHORT, short_liq + 1, short_liq)


def test_risk_based_notional_respects_risk():
    # risk 1% of 10000 = 100 USDT; stop 2% away -> notional = 100/0.02 = 5000
    n = pm.risk_based_notional(10_000, 1.0, 100, 98, leverage=10, max_notional_frac=0.5)
    assert n == pytest.approx(5000)


def test_risk_based_notional_capped_by_margin():
    # tiny stop distance would blow up notional; cap = balance*frac*lev
    n = pm.risk_based_notional(10_000, 1.0, 100, 99.99, leverage=10, max_notional_frac=0.5)
    assert n == pytest.approx(10_000 * 0.5 * 10)  # 50,000 cap


def test_risk_based_notional_guards():
    assert pm.risk_based_notional(0, 1.0, 100, 98, leverage=10) == 0.0
    assert pm.risk_based_notional(10_000, 1.0, 0, 98, leverage=10) == 0.0
