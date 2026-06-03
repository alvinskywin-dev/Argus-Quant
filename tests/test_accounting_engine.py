"""
Sprint 21E — unit tests for the pure net-PnL accounting math.

DB rollups (record_trade_accounting / daily) are exercised manually; the
correctness of the money math is pure and fully covered here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.accounting import pnl as m


def test_net_pnl_subtracts_all_costs():
    bd = m.compute_net_pnl(
        100.0,
        commission=2.0,
        funding_fee=1.0,
        slippage=0.5,
        commission_known=True,
        funding_known=True,
        slippage_known=True,
    )
    assert bd.gross_pnl == 100.0
    assert bd.net_pnl == 96.5  # 100 - 2 - 1 - 0.5
    assert bd.total_fees == 3.5  # commission + slippage + |funding|
    assert bd.estimate_quality == m.EXACT


def test_net_roe_uses_margin():
    bd = m.compute_net_pnl(
        50.0,
        commission=0.0,
        margin=500.0,
        commission_known=True,
        funding_known=True,
        slippage_known=True,
    )
    assert bd.net_roe == 10.0  # 50 / 500 * 100


def test_funding_convention_positive_is_paid():
    # Convention: positive funding_fee == funding PAID (a cost) -> reduces net.
    paid = m.compute_net_pnl(
        10.0, funding_fee=2.0, funding_known=True, commission_known=True, slippage_known=True
    )
    assert paid.net_pnl == 8.0  # 10 - 2 = 8


def test_funding_convention_negative_is_received():
    # Negative funding_fee == funding RECEIVED -> increases net.
    received = m.compute_net_pnl(
        10.0, funding_fee=-2.0, funding_known=True, commission_known=True, slippage_known=True
    )
    assert received.net_pnl == 12.0  # 10 - (-2) = 12


def test_estimate_quality_partial_when_some_unknown():
    bd = m.compute_net_pnl(
        100.0, commission=2.0, commission_known=False, funding_known=True, slippage_known=True
    )
    assert bd.estimate_quality == m.PARTIAL


def test_estimate_quality_estimated_when_all_unknown():
    bd = m.compute_net_pnl(100.0, commission=2.0)
    assert bd.estimate_quality == m.ESTIMATED


def test_estimate_commission_round_trip():
    # 1000 notional, 0.04% taker, both legs -> 0.8
    assert round(m.estimate_commission(1000.0), 4) == 0.8
    assert round(m.estimate_commission(1000.0, round_trip=False), 4) == 0.4


def test_slippage_cost():
    # filled 101 vs expected 100, qty 3 -> 3.0
    assert m.slippage_cost(100.0, 101.0, 3.0) == 3.0
    assert m.slippage_cost(0.0, 101.0, 3.0) == 0.0  # guard


def test_holding_seconds():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=5)
    assert m.holding_seconds(t0, t1) == 300
    assert m.holding_seconds(None, t1) == 0
    assert m.holding_seconds(t1, t0) == 0  # negative guarded to 0


def test_loss_is_negative_net():
    bd = m.compute_net_pnl(
        -20.0, commission=1.0, commission_known=True, funding_known=True, slippage_known=True
    )
    assert bd.net_pnl == -21.0
