"""
Lifecycle realized-PnL (partial booking + SL→break-even).

The audit bug: a TP-then-SL trade was booked as a full stop loss, so realized
PnL disagreed with the lifecycle win-rate. These pure-function tests pin the
blended-PnL contract that fixes it.
"""

from __future__ import annotations

import pytest

from app.analytics.lifecycle_pnl import blended_realized_pnl, price_pct

# LONG ladder: +2% / +4% / +7%, stop -2%.
LONG = dict(side="LONG", entry=100.0, tp1=102.0, tp2=104.0, tp3=107.0, stop_loss=98.0)
# SHORT mirror.
SHORT = dict(side="SHORT", entry=100.0, tp1=98.0, tp2=96.0, tp3=93.0, stop_loss=102.0)

FRACS = dict(tp1_frac=0.30, tp2_frac=0.30, sl_to_breakeven_after_tp1=True)


def _pnl(levels, max_tp, final):
    return blended_realized_pnl(max_tp_hit=max_tp, final_event=final, **levels, **FRACS)


def test_price_pct_signs():
    assert price_pct("LONG", 100, 102) == pytest.approx(2.0)
    assert price_pct("LONG", 100, 98) == pytest.approx(-2.0)
    assert price_pct("SHORT", 100, 98) == pytest.approx(2.0)
    assert price_pct("SHORT", 100, 102) == pytest.approx(-2.0)


def test_clean_stop_no_tp_is_full_loss():
    # No take-profit ever reached → the whole position exits at the stop.
    assert _pnl(LONG, max_tp=0, final="SL") == pytest.approx(-2.0)
    assert _pnl(SHORT, max_tp=0, final="SL") == pytest.approx(-2.0)


def test_tp1_then_sl_books_partial_not_full_loss():
    # 30% booked at +2% = +0.6; remaining 70% exits at break-even (0).
    assert _pnl(LONG, max_tp=1, final="SL") == pytest.approx(0.6)
    assert _pnl(SHORT, max_tp=1, final="SL") == pytest.approx(0.6)


def test_tp2_then_sl():
    # 0.3*2 + 0.3*4 = 1.8 booked; 40% remainder at break-even.
    assert _pnl(LONG, max_tp=2, final="SL") == pytest.approx(1.8)
    assert _pnl(SHORT, max_tp=2, final="SL") == pytest.approx(1.8)


def test_full_run_to_tp3():
    # 0.3*2 + 0.3*4 + 0.4*7 = 4.6
    assert _pnl(LONG, max_tp=3, final="TP3") == pytest.approx(4.6)
    assert _pnl(SHORT, max_tp=3, final="TP3") == pytest.approx(4.6)


def test_breakeven_disabled_remainder_takes_full_stop():
    fr = dict(tp1_frac=0.30, tp2_frac=0.30, sl_to_breakeven_after_tp1=False)
    # 0.3*2 booked, 0.7 remainder at -2% → 0.6 - 1.4 = -0.8
    val = blended_realized_pnl(max_tp_hit=1, final_event="SL", **LONG, **fr)
    assert val == pytest.approx(-0.8)


def test_winner_is_never_worse_than_clean_loss():
    # Booking partials can only improve a TP-then-SL outcome vs a clean stop.
    clean = _pnl(LONG, max_tp=0, final="SL")
    assert _pnl(LONG, max_tp=1, final="SL") > clean
