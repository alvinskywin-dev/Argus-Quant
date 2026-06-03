"""Sprint 22F — Liquidity Map V2 (pure analytic over candle data)."""

from __future__ import annotations

from app.liquidity.liquidity_map_v2 import (
    LiquidityMap,
    LiquidityNode,
    build_liquidity_map,
    liquidity_score_adjustment,
)


def _candle(o, h, low, c, v=1000.0):
    return {"open": o, "high": h, "low": low, "close": c, "volume": v}


def _series(n=40, base=100.0):
    """A simple oscillating series with two equal-high touches near the top."""
    out = []
    price = base
    for i in range(n):
        price += (1 if i % 2 == 0 else -1) * 0.5
        out.append(_candle(price, price + 1, price - 1, price, 1000 + i * 10))
    return out


def test_too_few_candles_returns_empty():
    lm = build_liquidity_map([_candle(100, 101, 99, 100)] * 5)
    assert isinstance(lm, LiquidityMap)
    assert lm.nodes == []
    assert lm.liquidity_score == 0


def test_basic_map_has_current_price():
    candles = _series()
    lm = build_liquidity_map(candles, symbol="BTCUSDT")
    assert lm.symbol == "BTCUSDT"
    assert lm.current_price == candles[-1]["close"]


def test_equal_highs_detected():
    candles = _series(40)
    # inject two equal highs at 130 in the window
    candles[10]["high"] = 130.0
    candles[20]["high"] = 130.02
    lm = build_liquidity_map(candles)
    eqh = [n for n in lm.nodes if n.kind == "EQH"]
    assert any(abs(n.price - 130.0) < 0.5 for n in eqh)


def test_equal_lows_detected():
    candles = _series(40)
    candles[12]["low"] = 70.0
    candles[24]["low"] = 70.01
    lm = build_liquidity_map(candles)
    eql = [n for n in lm.nodes if n.kind == "EQL"]
    assert any(abs(n.price - 70.0) < 0.5 for n in eql)


def test_prev_day_week_levels_injected():
    candles = _series()
    lm = build_liquidity_map(
        candles, prev_day={"high": 200, "low": 50}, prev_week={"high": 300, "low": 20}
    )
    kinds = {n.kind for n in lm.nodes}
    assert {"PDH", "PDL", "PWH", "PWL"} <= kinds


def test_poc_present_with_volume():
    lm = build_liquidity_map(_series())
    assert any(n.kind == "POC" for n in lm.nodes)


def test_magnets_above_below():
    candles = _series()
    lm = build_liquidity_map(candles, prev_day={"high": 200, "low": 50})
    if lm.nearest_magnet_above is not None:
        assert lm.nearest_magnet_above > lm.current_price
    if lm.nearest_magnet_below is not None:
        assert lm.nearest_magnet_below < lm.current_price


def test_sweep_up_detected():
    candles = _series(40)
    # establish equal highs at 120
    candles[10]["high"] = 120.0
    candles[20]["high"] = 120.0
    # last candle wicks above 120 but closes below -> sweep up
    candles[-1] = _candle(118, 121.0, 117, 118.5)
    lm = build_liquidity_map(candles)
    assert lm.sweep_detected is True
    assert lm.sweep_direction == "UP"
    assert lm.bias == "SHORT"


def test_sweep_down_detected():
    candles = _series(40)
    candles[10]["low"] = 80.0
    candles[20]["low"] = 80.0
    candles[-1] = _candle(82, 83, 79.0, 81.5)  # wick below 80, close above
    lm = build_liquidity_map(candles)
    assert lm.sweep_detected is True
    assert lm.sweep_direction == "DOWN"
    assert lm.bias == "LONG"


def test_score_bounded():
    lm = build_liquidity_map(_series())
    assert 0 <= lm.liquidity_score <= 100


def test_score_adjustment_agrees_with_bias():
    candles = _series(40)
    candles[10]["low"] = 80.0
    candles[20]["low"] = 80.0
    candles[-1] = _candle(82, 83, 79.0, 81.5)  # bullish sweep
    lm = build_liquidity_map(candles)
    delta_long = liquidity_score_adjustment(lm, "LONG")
    delta_short = liquidity_score_adjustment(lm, "SHORT")
    assert delta_long > delta_short


def test_score_adjustment_empty_map_zero():
    assert liquidity_score_adjustment(LiquidityMap(), "LONG") == 0


def test_magnet_for_side():
    lm = LiquidityMap(current_price=100, nearest_magnet_above=110, nearest_magnet_below=90)
    assert lm.magnet_for_side("LONG") == 110
    assert lm.magnet_for_side("SHORT") == 90


def test_stop_zone_for_side():
    lm = LiquidityMap(
        current_price=100,
        nearest_magnet_above=110,
        nearest_magnet_below=90,
        stop_hunt_zones=[85, 95, 105, 115],
    )
    assert lm.stop_zone_for_side("LONG") == 95  # highest below price
    assert lm.stop_zone_for_side("SHORT") == 105  # lowest above price


def test_accepts_list_of_sequences():
    rows = [[100, 101, 99, 100, 1000] for _ in range(30)]
    lm = build_liquidity_map(rows)
    assert isinstance(lm, LiquidityMap)


def test_to_dict_serialisable():
    lm = build_liquidity_map(_series())
    d = lm.to_dict()
    assert "nodes" in d and isinstance(d["nodes"], list)
    if d["nodes"]:
        assert "price" in d["nodes"][0]


def test_node_dataclass():
    n = LiquidityNode(price=100, kind="EQH", touches=3)
    assert n.to_dict()["kind"] == "EQH"
