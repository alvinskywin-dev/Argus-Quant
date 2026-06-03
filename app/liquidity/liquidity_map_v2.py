"""
Sprint 22F — Liquidity Map V2.

Upgrades the liquidity picture from single-pattern detection to a structured map
of where resting liquidity sits and how it should bias a trade:

  • equal-highs / equal-lows clustering (multi-touch pools)
  • previous-day and weekly high / low (classic liquidity magnets)
  • volume-profile zones + point of control (high-liquidity node)
  • liquidity-sweep scoring (was a pool just swept and reclaimed?)
  • stop-hunt zones and magnet targets

It is a pure analytic over candle data (a list of OHLCV dicts, or a pandas
DataFrame — both accepted). It returns a `LiquidityMap` whose `score`/`bias`
the caller may fold into confidence, TP selection and SL placement. With
`LIQUIDITY_MAP_V2_ENABLED=false` the caller should skip it; the function itself
is side-effect-free and safe to call regardless.

No fake data: with too few candles it returns an empty, well-formed map.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional, Sequence

_TOLERANCE_PCT = 0.12  # two extremes within this % are "equal"
_MIN_CANDLES = 20


@dataclass
class LiquidityNode:
    price: float
    kind: str  # EQH | EQL | PDH | PDL | PWH | PWL | POC | HVN
    touches: int = 1
    strength: float = 0.0  # 0-1 relative weight
    side: str = ""  # ABOVE | BELOW (relative to current price)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LiquidityMap:
    symbol: str = ""
    current_price: float = 0.0
    nodes: List[LiquidityNode] = field(default_factory=list)
    nearest_magnet_above: Optional[float] = None
    nearest_magnet_below: Optional[float] = None
    sweep_detected: bool = False
    sweep_direction: str = ""  # UP | DOWN
    sweep_probability: float = 0.0  # 0-1
    stop_hunt_zones: List[float] = field(default_factory=list)
    liquidity_score: int = 0  # 0-100
    bias: str = "NEUTRAL"  # LONG | SHORT | NEUTRAL

    def magnet_for_side(self, side: str) -> Optional[float]:
        """The TP-side liquidity magnet for a LONG/SHORT."""
        return (
            self.nearest_magnet_above
            if (side or "").upper() == "LONG"
            else self.nearest_magnet_below
        )

    def stop_zone_for_side(self, side: str) -> Optional[float]:
        """The SL-side stop-hunt zone for a LONG/SHORT (where stops rest)."""
        if (side or "").upper() == "LONG":
            below = [z for z in self.stop_hunt_zones if z < self.current_price]
            return max(below) if below else self.nearest_magnet_below
        above = [z for z in self.stop_hunt_zones if z > self.current_price]
        return min(above) if above else self.nearest_magnet_above

    def to_dict(self) -> dict:
        d = asdict(self)
        d["nodes"] = [n.to_dict() if isinstance(n, LiquidityNode) else n for n in self.nodes]
        return d


def _extract(candles) -> tuple[list, list, list, list, list]:
    """Return (opens, highs, lows, closes, volumes) from a DataFrame or list of
    dicts/sequences. Degrades to empty lists if shape is unknown."""
    # pandas DataFrame
    if hasattr(candles, "columns"):
        try:
            return (
                list(candles["open"].values),
                list(candles["high"].values),
                list(candles["low"].values),
                list(candles["close"].values),
                list(candles["volume"].values) if "volume" in candles.columns else [],
            )
        except Exception:
            return [], [], [], [], []
    o, h, lw, c, v = [], [], [], [], []
    for row in candles or []:
        if isinstance(row, dict):
            o.append(float(row.get("open", 0)))
            h.append(float(row.get("high", 0)))
            lw.append(float(row.get("low", 0)))
            c.append(float(row.get("close", 0)))
            v.append(float(row.get("volume", 0)))
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            o.append(float(row[0]))
            h.append(float(row[1]))
            lw.append(float(row[2]))
            c.append(float(row[3]))
            v.append(float(row[4]))
    return o, h, lw, c, v


def _cluster_levels(values: Sequence[float], tol: float) -> List[tuple]:
    """Group near-equal levels. Returns [(level, touch_count)] sorted by touches."""
    clusters: List[list] = []
    for val in values:
        placed = False
        for cl in clusters:
            if abs(cl[0] - val) <= tol:
                cl[1] += 1
                cl[0] = (cl[0] * (cl[1] - 1) + val) / cl[1]  # running mean
                placed = True
                break
        if not placed:
            clusters.append([val, 1])
    out = [(round(c[0], 10), c[1]) for c in clusters if c[1] >= 2]
    return sorted(out, key=lambda x: x[1], reverse=True)


def _volume_profile(highs, lows, closes, volumes, bins: int = 24) -> Optional[float]:
    """Point of control (price of the highest-volume bin). None if no volume."""
    if not volumes or not closes:
        return None
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        return None
    width = (hi - lo) / bins
    buckets = [0.0] * bins
    for c, vol in zip(closes, volumes, strict=False):
        idx = min(bins - 1, max(0, int((c - lo) / width)))
        buckets[idx] += vol
    poc_idx = max(range(bins), key=lambda i: buckets[i])
    return round(lo + (poc_idx + 0.5) * width, 10)


def build_liquidity_map(
    candles,
    *,
    symbol: str = "",
    prev_day: Optional[dict] = None,
    prev_week: Optional[dict] = None,
    lookback: int = 60,
) -> LiquidityMap:
    """Build the liquidity map.

    ``candles``: intraday candles (DataFrame or list of OHLCV dicts), ascending.
    ``prev_day`` / ``prev_week``: optional ``{"high": .., "low": ..}`` for PDH/PDL,
    PWH/PWL. When omitted they are inferred from the candle extremes.
    """
    opens, highs, lows, closes, volumes = _extract(candles)
    lm = LiquidityMap(symbol=symbol)
    if len(closes) < _MIN_CANDLES:
        return lm

    price = float(closes[-1])
    lm.current_price = price
    tol = price * _TOLERANCE_PCT / 100.0
    win = min(lookback, len(highs) - 1)
    wh = highs[-win - 1 : -1]
    wl = lows[-win - 1 : -1]

    nodes: List[LiquidityNode] = []

    # Equal highs / lows clusters.
    for level, touches in _cluster_levels(wh, tol)[:4]:
        nodes.append(
            LiquidityNode(level, "EQH", touches, side="ABOVE" if level > price else "BELOW")
        )
    for level, touches in _cluster_levels(wl, tol)[:4]:
        nodes.append(
            LiquidityNode(level, "EQL", touches, side="ABOVE" if level > price else "BELOW")
        )

    # Previous day / week extremes.
    pdh = (
        (prev_day or {}).get("high")
        if prev_day
        else max(highs[-min(len(highs), 96) :], default=None)
    )
    pdl = (
        (prev_day or {}).get("low") if prev_day else min(lows[-min(len(lows), 96) :], default=None)
    )
    pwh = (prev_week or {}).get("high") if prev_week else max(highs, default=None)
    pwl = (prev_week or {}).get("low") if prev_week else min(lows, default=None)
    for lvl, kind in ((pdh, "PDH"), (pdl, "PDL"), (pwh, "PWH"), (pwl, "PWL")):
        if lvl:
            nodes.append(
                LiquidityNode(
                    round(float(lvl), 10), kind, 1, side="ABOVE" if lvl > price else "BELOW"
                )
            )

    # Volume profile POC (high-liquidity node).
    poc = _volume_profile(highs, lows, closes, volumes)
    if poc:
        nodes.append(LiquidityNode(poc, "POC", 1, side="ABOVE" if poc > price else "BELOW"))

    # Strength: normalise touch counts.
    max_touch = max((n.touches for n in nodes), default=1)
    for n in nodes:
        base = n.touches / max_touch
        if n.kind in ("PWH", "PWL", "POC"):
            base = min(1.0, base + 0.4)
        elif n.kind in ("PDH", "PDL"):
            base = min(1.0, base + 0.2)
        n.strength = round(base, 3)
    lm.nodes = sorted(nodes, key=lambda n: n.strength, reverse=True)

    above = [n.price for n in nodes if n.price > price]
    below = [n.price for n in nodes if n.price < price]
    lm.nearest_magnet_above = min(above) if above else None
    lm.nearest_magnet_below = max(below) if below else None
    # Stop-hunt zones: pools just beyond the nearest magnets.
    lm.stop_hunt_zones = sorted({round(x, 10) for x in (above[:2] + below[-2:])})

    # Sweep detection on the last candle: wick beyond a pool, body reclaimed.
    last_h, last_l, last_c = highs[-1], lows[-1], closes[-1]
    eqh_levels = [n.price for n in nodes if n.kind in ("EQH", "PDH")]
    eql_levels = [n.price for n in nodes if n.kind in ("EQL", "PDL")]
    swept_up = any(last_h > lvl + tol and last_c < lvl for lvl in eqh_levels)
    swept_down = any(last_l < lvl - tol and last_c > lvl for lvl in eql_levels)
    if swept_up:
        lm.sweep_detected = True
        lm.sweep_direction = "UP"  # liquidity above taken → bearish-to-bullish reversal lower? bias SHORT then reclaim
    elif swept_down:
        lm.sweep_detected = True
        lm.sweep_direction = "DOWN"

    # Sweep probability: proximity of price to the strongest unswept pool.
    strongest = lm.nodes[0] if lm.nodes else None
    if strongest and price:
        dist = abs(strongest.price - price) / price
        lm.sweep_probability = round(
            max(0.0, min(1.0, (0.01 - dist) / 0.01)) * strongest.strength, 3
        )

    # Score 0-100 and directional bias.
    score = 0.0
    score += min(40.0, sum(n.strength for n in nodes) * 12)
    if lm.sweep_detected:
        score += 25.0
    score += lm.sweep_probability * 35.0
    lm.liquidity_score = int(round(min(100.0, score)))

    # Bias: a swept-down pool that reclaimed is bullish; swept-up is bearish.
    if swept_down:
        lm.bias = "LONG"
    elif swept_up:
        lm.bias = "SHORT"
    else:
        # lean toward the side with the closer strong magnet (mean reversion).
        if lm.nearest_magnet_above and lm.nearest_magnet_below:
            up = lm.nearest_magnet_above - price
            dn = price - lm.nearest_magnet_below
            lm.bias = "LONG" if up < dn else "SHORT" if dn < up else "NEUTRAL"
    return lm


def liquidity_score_adjustment(lm: LiquidityMap, side: str) -> int:
    """Confidence delta (-10..+15) the caller may add for ``side`` given the map.
    Positive when the map agrees with the trade direction."""
    if not lm.nodes:
        return 0
    side = (side or "").upper()
    delta = 0
    if lm.bias == side:
        delta += int(round(lm.liquidity_score / 10))
    elif lm.bias not in ("NEUTRAL", "") and lm.bias != side:
        delta -= 5
    if lm.sweep_detected and (
        (side == "LONG" and lm.sweep_direction == "DOWN")
        or (side == "SHORT" and lm.sweep_direction == "UP")
    ):
        delta += 5
    return max(-10, min(15, delta))
