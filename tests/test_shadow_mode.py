"""Sprint 22G — Shadow Mode Live Validation.

Includes a SAFETY test asserting the module can never place real orders.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.shadow import shadow_mode
from app.shadow.shadow_mode import (
    ShadowFill,
    ShadowResult,
    build_report,
    simulate_entry_fill,
    simulate_signal,
)


@pytest.fixture
def cfg():
    keys = ["shadow_mode_enabled", "shadow_mode_slippage_bps", "shadow_mode_latency_ms"]
    saved = {k: getattr(settings, k) for k in keys}
    settings.shadow_mode_enabled = True
    settings.shadow_mode_slippage_bps = 5.0
    settings.shadow_mode_latency_ms = 250.0
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _sig(**over):
    base = {"symbol": "BTCUSDT", "side": "LONG", "entry": 100.0, "tp1": 110.0, "stop_loss": 95.0}
    base.update(over)
    return base


# ══════════════════════════════════════════════════════════════════════════
#  SAFETY — the hard guarantee
# ══════════════════════════════════════════════════════════════════════════
def test_module_declares_no_real_orders():
    assert shadow_mode.__places_real_orders__ is False


def test_module_does_not_import_exchange_client():
    import inspect

    src = inspect.getsource(shadow_mode)
    banned = ["exchange_adapters", "binance_client", "place_order", "create_order", "get_client"]
    for token in banned:
        assert token not in src, f"shadow_mode must not reference {token}"


# ══════════════════════════════════════════════════════════════════════════
#  entry fill simulation
# ══════════════════════════════════════════════════════════════════════════
def test_long_slippage_adverse(cfg):
    fill = simulate_entry_fill("LONG", 100.0)
    assert fill.fill_price > 100.0  # pay up
    assert fill.latency_ms == 250.0


def test_short_slippage_adverse(cfg):
    fill = simulate_entry_fill("SHORT", 100.0)
    assert fill.fill_price < 100.0  # sell lower


def test_miss_probability_when_price_gaps_away(cfg):
    # LONG limit at 100, next candle never trades below 102 -> likely missed
    fill = simulate_entry_fill("LONG", 100.0, next_candle={"low": 102, "high": 105})
    assert fill.miss_probability > 0
    assert fill.filled is False


def test_fill_when_price_trades_through(cfg):
    fill = simulate_entry_fill("LONG", 100.0, next_candle={"low": 99, "high": 101})
    assert fill.filled is True
    assert fill.miss_probability == 0


# ══════════════════════════════════════════════════════════════════════════
#  signal simulation
# ══════════════════════════════════════════════════════════════════════════
def test_long_hits_tp(cfg):
    r = simulate_signal(_sig(), price_path=[101, 105, 111])
    assert r.outcome == "TP"
    assert r.hypothetical_pnl_percent > 0


def test_long_hits_sl(cfg):
    r = simulate_signal(_sig(), price_path=[99, 96, 94])
    assert r.outcome == "SL"
    assert r.hypothetical_pnl_percent < 0


def test_short_hits_tp(cfg):
    r = simulate_signal(_sig(side="SHORT", tp1=90, stop_loss=105), price_path=[98, 92, 89])
    assert r.outcome == "TP"
    assert r.hypothetical_pnl_percent > 0


def test_open_when_no_touch(cfg):
    r = simulate_signal(_sig(), price_path=[100.5, 101, 100.2])
    assert r.outcome == "OPEN"


def test_actual_move_recorded(cfg):
    r = simulate_signal(_sig(), price_path=[101, 105, 108])
    assert r.actual_move_percent == 8.0


def test_paper_pnl_carried(cfg):
    r = simulate_signal(_sig(), price_path=[111], paper_pnl_percent=9.5)
    assert r.paper_pnl_percent == 9.5


def test_tp_sl_sync_ok_long(cfg):
    r = simulate_signal(_sig(), price_path=[100.5])
    assert r.tp_sl_sync_ok is True


def test_tp_sl_sync_bad(cfg):
    # tp below entry for a LONG -> invalid geometry
    r = simulate_signal(_sig(tp1=90, stop_loss=95), price_path=[100.5])
    assert r.tp_sl_sync_ok is False


def test_entry_from_low_high(cfg):
    r = simulate_signal(
        {
            "symbol": "X",
            "side": "LONG",
            "entry_low": 99,
            "entry_high": 101,
            "tp1": 110,
            "stop_loss": 95,
        },
        price_path=[110],
    )
    assert r.hypothetical_entry > 0


def test_result_to_dict(cfg):
    r = simulate_signal(_sig(), price_path=[111])
    d = r.to_dict()
    assert d["outcome"] == "TP"
    assert isinstance(d["entry_fill"], dict)


# ══════════════════════════════════════════════════════════════════════════
#  reporting (shadow vs paper)
# ══════════════════════════════════════════════════════════════════════════
def test_report_empty():
    rep = build_report([])
    assert rep.sample_size == 0
    assert rep.shadow_winrate == 0.0


def test_report_winrate(cfg):
    results = [
        simulate_signal(_sig(), price_path=[111], paper_pnl_percent=9),
        simulate_signal(_sig(), price_path=[94], paper_pnl_percent=-5),
        simulate_signal(_sig(), price_path=[112], paper_pnl_percent=10),
    ]
    rep = build_report(results)
    assert rep.sample_size == 3
    assert rep.shadow_winrate == pytest.approx(66.67, abs=0.1)
    assert rep.paper_winrate is not None
    assert 0 <= rep.tp_sl_sync_realism <= 1


def test_report_slippage_impact(cfg):
    results = [simulate_signal(_sig(), price_path=[111], paper_pnl_percent=10)]
    rep = build_report(results)
    # paper assumed better than slippage-adjusted shadow
    assert rep.slippage_impact_percent != 0.0
    assert rep.avg_latency_ms == 250.0


def test_fill_to_dict():
    f = ShadowFill(requested_price=100, fill_price=100.05, slippage_percent=0.05, latency_ms=250)
    assert f.to_dict()["latency_ms"] == 250


def test_is_enabled(cfg):
    assert shadow_mode.is_enabled() is True
    cfg.shadow_mode_enabled = False
    assert shadow_mode.is_enabled() is False
