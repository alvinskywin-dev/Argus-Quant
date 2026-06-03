"""
Paper Trading ROE/PnL realtime calculation — regression tests.

Covers the bug where open positions always showed ROE 0.00% / PnL $0.00 /
Mark == Entry because the price cache only tracked 3 hardcoded symbols and the
mark-price helper silently fell back to entry_price.

Verifies:
  * the canonical ROE/PnL formulas for long & short at 10x,
  * mark_price NEVER defaults to entry_price (returns 0.0 when absent),
  * _position_out reflects live marks (price up / price down) and emits None
    — not a phantom 0 — when no live mark exists.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.paper_engine import math as pm
from app.paper_engine import service
from app.paper_engine.router import _position_out

# ── canonical formulas (the spec) ──────────────────────────────────
# LONG : roe = (mark-entry)/entry * lev * 100 ; pnl = (mark-entry) * qty
# SHORT: roe = (entry-mark)/entry * lev * 100 ; pnl = (entry-mark) * qty

@pytest.mark.parametrize("side,entry,mark", [
    ("LONG", 100.0, 110.0),   # +10%
    ("LONG", 100.0, 90.0),    # -10%
    ("SHORT", 100.0, 90.0),   # +10% in favour
    ("SHORT", 100.0, 110.0),  # -10% against
])
def test_roe_and_pnl_match_spec_at_10x(side, entry, mark):
    lev = 10
    notional = 1000.0
    qty = pm.position_quantity(notional, entry)      # 10 units
    margin = pm.required_margin(notional, lev)        # 100 USDT

    pnl = pm.unrealized_pnl(side, entry, mark, notional)
    roe = pm.roe_pct(pnl, margin)

    if side == "LONG":
        exp_pnl = (mark - entry) * qty
        exp_roe = (mark - entry) / entry * lev * 100
    else:
        exp_pnl = (entry - mark) * qty
        exp_roe = (entry - mark) / entry * lev * 100

    assert pnl == pytest.approx(exp_pnl)
    assert roe == pytest.approx(exp_roe)


def test_long_10x_plus_10pct_is_100pct_roe():
    pnl = pm.unrealized_pnl("LONG", 100, 110, 1000)
    assert pm.roe_pct(pnl, 100) == pytest.approx(100.0)


def test_short_10x_plus_10pct_is_100pct_roe():
    pnl = pm.unrealized_pnl("SHORT", 100, 90, 1000)
    assert pm.roe_pct(pnl, 100) == pytest.approx(100.0)


# ── mark_price must never fall back to entry ───────────────────────

def test_mark_price_returns_zero_when_absent(monkeypatch):
    from app.market_data import ws_engine
    monkeypatch.setattr(ws_engine, "latest_prices", {}, raising=False)
    assert service.mark_price("FOOUSDT") == 0.0  # NOT the entry price


def test_mark_price_reads_live_cache(monkeypatch):
    from app.market_data import ws_engine
    monkeypatch.setattr(ws_engine, "latest_prices", {"BTCUSDT": 65000.0}, raising=False)
    assert service.mark_price("BTCUSDT") == 65000.0


# ── _position_out integration: realtime mark drives ROE/PnL ────────

def _pos(symbol="BTCUSDT", side="LONG", entry=100.0):
    return SimpleNamespace(
        id=1, signal_id=None, symbol=symbol, side=side, entry_price=entry,
        quantity=10.0, notional_usdt=1000.0, leverage=10, margin_usdt=100.0,
        liquidation_price=90.0, stop_loss=None, tp1=None, tp2=None, tp3=None,
        status="OPEN", realized_pnl_usdt=0.0, funding_usdt=0.0,
        opened_at=None, closed_at=None,
    )


def test_position_out_price_up(monkeypatch):
    from app.market_data import ws_engine
    monkeypatch.setattr(ws_engine, "latest_prices", {"BTCUSDT": 110.0}, raising=False)
    out = _position_out(_pos(side="LONG", entry=100.0))
    assert out.mark_price == 110.0
    assert out.unrealized_pnl == pytest.approx(100.0)   # (110-100)*10
    assert out.roe_pct == pytest.approx(100.0)          # 10% * 10x


def test_position_out_price_down(monkeypatch):
    from app.market_data import ws_engine
    monkeypatch.setattr(ws_engine, "latest_prices", {"BTCUSDT": 95.0}, raising=False)
    out = _position_out(_pos(side="LONG", entry=100.0))
    assert out.mark_price == 95.0
    assert out.unrealized_pnl == pytest.approx(-50.0)   # (95-100)*10
    assert out.roe_pct == pytest.approx(-50.0)


def test_position_out_short_price_down(monkeypatch):
    from app.market_data import ws_engine
    monkeypatch.setattr(ws_engine, "latest_prices", {"ETHUSDT": 90.0}, raising=False)
    out = _position_out(_pos(symbol="ETHUSDT", side="SHORT", entry=100.0))
    assert out.mark_price == 90.0
    assert out.unrealized_pnl == pytest.approx(100.0)
    assert out.roe_pct == pytest.approx(100.0)


def test_position_out_no_live_mark_is_none_not_zero(monkeypatch):
    """When there is no live price the card shows '—' (None), never entry/0%."""
    from app.market_data import ws_engine
    monkeypatch.setattr(ws_engine, "latest_prices", {}, raising=False)
    out = _position_out(_pos(symbol="NOPRICEUSDT", side="LONG", entry=100.0))
    assert out.mark_price is None
    assert out.unrealized_pnl is None
    assert out.roe_pct is None
