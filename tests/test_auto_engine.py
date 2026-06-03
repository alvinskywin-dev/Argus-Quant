"""
Sprint 20D — unit tests for auto-engine risk + protection logic (no DB).
"""

from __future__ import annotations

import pytest

from app.auto_engine import risk
from app.paper_engine import math as pm


def _eval(**over):
    base = dict(
        enabled=True,
        symbol="BTCUSDT",
        side="LONG",
        confidence=90.0,
        open_positions=0,
        available_margin=1000.0,
        max_positions=5,
        max_leverage=10,
        risk_per_trade_pct=1.0,
        allowed_coins="",
        allowed_exchanges="",
        min_confidence=0.0,
    )
    base.update(over)
    return risk.evaluate(**base)


def test_base_coin():
    assert risk.base_coin("BTCUSDT") == "BTC"
    assert risk.base_coin("ETHUSDC") == "ETH"
    assert risk.base_coin("1000PEPEUSDT") == "1000PEPE"


def test_allow_normal():
    d = _eval()
    assert d.allow and d.leverage == 10 and d.risk_pct == 1.0


def test_deny_disabled():
    assert not _eval(enabled=False).allow


def test_deny_below_min_confidence():
    assert not _eval(min_confidence=95, confidence=90).allow
    assert _eval(min_confidence=85, confidence=90).allow


def test_deny_coin_not_allowed():
    assert not _eval(allowed_coins="ETH,SOL").allow
    assert _eval(allowed_coins="BTC,ETH").allow


def test_deny_max_positions():
    assert not _eval(open_positions=5, max_positions=5).allow
    assert _eval(open_positions=4, max_positions=5).allow


def test_deny_no_margin():
    assert not _eval(available_margin=0).allow


def test_leverage_clamped_to_max():
    assert _eval(max_leverage=3).leverage == 3


def test_deny_exchange_restriction_without_connection():
    assert not _eval(allowed_exchanges="binance", has_connected_exchange=False).allow
    assert _eval(allowed_exchanges="binance", has_connected_exchange=True).allow


def test_trailing_stop_direction():
    # long: stop trails below reference; short: above
    assert risk.trailing_stop(pm.LONG, 100, 1.0) == pytest.approx(99.0)
    assert risk.trailing_stop(pm.SHORT, 100, 1.0) == pytest.approx(101.0)


def test_tighten_stop_only_moves_favourably():
    # long: stop only moves up
    assert risk.tighten_stop(pm.LONG, 98, 99) == 99
    assert risk.tighten_stop(pm.LONG, 99, 98) == 99
    # short: stop only moves down
    assert risk.tighten_stop(pm.SHORT, 102, 101) == 101
    assert risk.tighten_stop(pm.SHORT, 101, 102) == 101
    # no prior stop
    assert risk.tighten_stop(pm.LONG, 0, 95) == 95
