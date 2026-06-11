"""
Paper sizing de-duplication (D1).

`paper.trading._calc_size` was a standalone re-implementation of the risk-based
sizing already in `paper_engine.math.risk_based_notional`. It now delegates to
that single source. This test pins byte-exact equivalence to the ORIGINAL
formula across normal, cap-hit, and degenerate inputs so the refactor cannot
change paper-follow position sizes.
"""

from __future__ import annotations

import pytest

from app.paper.trading import RISK_PCT, _calc_size


def _original(entry: float, stop_loss: float, balance: float) -> float:
    """Verbatim copy of the pre-refactor _calc_size — the equivalence oracle."""
    risk_usdt = balance * RISK_PCT
    if entry <= 0 or stop_loss <= 0:
        return round(risk_usdt, 2)
    dist = abs(entry - stop_loss) / entry
    if dist <= 0:
        return round(risk_usdt, 2)
    return round(min(risk_usdt / dist, balance * 0.5), 2)


_GRID = [
    # (entry, stop_loss, balance)
    (100.0, 98.0, 10_000.0),  # normal 2% stop
    (100.0, 95.0, 10_000.0),  # wider stop
    (100.0, 99.9, 10_000.0),  # tight stop → cap binds (0.5 * balance)
    (50_000.0, 49_000.0, 25_000.0),  # BTC-ish
    (0.5, 0.48, 2_500.0),  # low-priced alt
    (100.0, 100.0, 10_000.0),  # degenerate: entry == stop
    (100.0, 0.0, 10_000.0),  # no stop
    (0.0, 98.0, 10_000.0),  # no entry
    (100.0, 102.0, 1_000.0),  # SHORT-side stop above entry
    (123.45, 120.0, 7_777.0),  # arbitrary
]


@pytest.mark.parametrize("entry,stop,balance", _GRID)
def test_calc_size_matches_original_formula(entry, stop, balance):
    assert _calc_size(entry, stop, balance) == _original(entry, stop, balance)


def test_cap_is_half_balance():
    # A very tight stop would imply a huge notional; it must cap at 50% balance.
    assert _calc_size(100.0, 99.99, 10_000.0) == 5_000.0


def test_normal_case_value():
    # 1% of 10k = 100 risk over a 2% stop → 5_000 notional.
    assert _calc_size(100.0, 98.0, 10_000.0) == pytest.approx(5_000.0)
