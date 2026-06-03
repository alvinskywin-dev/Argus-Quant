"""Sprint 22C — Trade Lifecycle Analytics (pure computation)."""

from __future__ import annotations

from app.analytics.trade_lifecycle import (
    LifecycleAggregate,
    TradeLifecycle,
    aggregate_lifecycles,
    compute_lifecycle,
)


def _trade(**over):
    base = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "tp1": 110.0,
        "status": "TP1",
        "max_favorable_pct": 12.0,
        "max_adverse_pct": 2.0,
        "time_to_tp1_seconds": 3600,
    }
    base.update(over)
    return base


def test_basic_lifecycle_from_fields():
    lc = compute_lifecycle(_trade())
    assert lc.symbol == "BTCUSDT"
    assert lc.mfe_percent == 12.0
    assert lc.mae_percent == 2.0
    assert lc.outcome == "TP1"


def test_mfe_mae_from_price_path():
    # entry 100, path goes to 115 then back to 98
    lc = compute_lifecycle(
        {
            "symbol": "X",
            "side": "LONG",
            "entry": 100,
            "stop_loss": 90,
            "tp1": 120,
            "status": "OPEN",
        },
        price_path=[101, 108, 115, 110, 98],
    )
    assert lc.mfe_percent == 15.0
    assert lc.mae_percent == 2.0  # 100 -> 98
    assert lc.volatility_during_trade > 0


def test_short_favorable_direction():
    lc = compute_lifecycle(
        {
            "symbol": "X",
            "side": "SHORT",
            "entry": 100,
            "stop_loss": 105,
            "tp1": 90,
            "status": "OPEN",
        },
        price_path=[98, 95, 92],
    )
    # price falling is favourable for SHORT
    assert lc.mfe_percent == 8.0


def test_entry_quality_high_when_low_mae():
    lc = compute_lifecycle(_trade(max_adverse_pct=0.0))  # risk = 5%
    assert lc.entry_quality_score == 100.0


def test_entry_quality_low_when_near_stop():
    lc = compute_lifecycle(_trade(max_adverse_pct=5.0))  # MAE == risk
    assert lc.entry_quality_score == 0.0


def test_sl_quality_penalised_when_stopped_but_mfe_big():
    lc = compute_lifecycle(_trade(status="SL", max_favorable_pct=4.5, max_adverse_pct=5.0))
    # got stopped after running most of the way to target -> too tight -> low score
    assert lc.sl_quality_score < 30


def test_tp_quality_captures_move_fraction():
    # reward 10% (entry 100 -> tp 110), MFE 20 -> captured half
    lc = compute_lifecycle(_trade(tp1=110, max_favorable_pct=20.0))
    assert lc.tp_quality_score == 50.0


def test_recovery_after_drawdown():
    lc = compute_lifecycle(_trade(status="TP1", max_adverse_pct=3.0, max_favorable_pct=12.0))
    assert lc.recovery_after_drawdown is True


def test_empty_trade_no_crash():
    lc = compute_lifecycle({})
    assert isinstance(lc, TradeLifecycle)
    assert lc.outcome == "CLOSED"


def test_entry_from_low_high_mid():
    lc = compute_lifecycle(
        {
            "side": "LONG",
            "entry_low": 99,
            "entry_high": 101,
            "stop_loss": 95,
            "tp1": 110,
            "max_favorable_pct": 5,
            "max_adverse_pct": 1,
            "status": "TP1",
        }
    )
    assert lc.entry_quality_score > 0


def test_opt_int_parsing():
    lc = compute_lifecycle(_trade(time_to_tp1_seconds="7200"))
    assert lc.time_to_tp1_seconds == 7200


def test_opt_int_bad_value():
    lc = compute_lifecycle(_trade(time_to_tp1_seconds="abc"))
    assert lc.time_to_tp1_seconds is None


# ── aggregation ──────────────────────────────────────────────────────────────
def test_aggregate_empty():
    agg = aggregate_lifecycles([])
    assert isinstance(agg, LifecycleAggregate)
    assert agg.sample_size == 0


def test_aggregate_basic():
    lcs = [
        compute_lifecycle(_trade(status="TP1", max_favorable_pct=10, max_adverse_pct=2)),
        compute_lifecycle(_trade(status="SL", max_favorable_pct=3, max_adverse_pct=5)),
        compute_lifecycle(_trade(status="TP2", max_favorable_pct=15, max_adverse_pct=1)),
    ]
    agg = aggregate_lifecycles(lcs)
    assert agg.sample_size == 3
    assert agg.avg_mfe > 0
    assert agg.avg_mfe_before_sl == 3.0  # the one SL trade
    assert agg.optimal_tp_distance_percent is not None
    assert agg.optimal_sl_distance_percent is not None


def test_aggregate_regime_breakdown():
    lcs = [
        compute_lifecycle(_trade(status="TP1", max_favorable_pct=10)),
        compute_lifecycle(_trade(status="SL", max_favorable_pct=3, max_adverse_pct=5)),
    ]
    agg = aggregate_lifecycles(lcs, regimes=["BULL", "HIGH_VOLATILITY"])
    assert "BULL" in agg.regime_performance
    assert "HIGH_VOLATILITY" in agg.regime_performance
    assert agg.regime_performance["BULL"]["trades"] == 1


def test_aggregate_to_dict():
    agg = aggregate_lifecycles([compute_lifecycle(_trade())])
    d = agg.to_dict()
    assert "avg_mfe" in d
    assert "regime_performance" in d
    assert isinstance(d["regime_performance"], dict)


def test_optimal_sl_covers_winner_mae():
    lcs = [compute_lifecycle(_trade(status="TP1", max_adverse_pct=3.0))]
    agg = aggregate_lifecycles(lcs)
    # optimal SL ≈ worst winner MAE * 1.1
    assert agg.optimal_sl_distance_percent >= 3.0
