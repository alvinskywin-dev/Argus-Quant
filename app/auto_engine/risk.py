"""
Sprint 20D — pure risk-check + protection maths for the auto engine.

No DB, no I/O — just decisions, so every branch is unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.paper_engine import math as pmath


@dataclass
class RiskDecision:
    allow: bool
    reason: str
    leverage: int = 0
    risk_pct: float = 0.0


def base_coin(symbol: str) -> str:
    """BTCUSDT -> BTC, ETHUSDT -> ETH (strips common quote suffixes)."""
    s = symbol.upper()
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def _csv_set(value: str) -> set[str]:
    return {x.strip().upper() for x in (value or "").split(",") if x.strip()}


def scale_leverage(
    confidence: float,
    max_leverage: int,
    *,
    floor_confidence: float = 75.0,
    full_confidence: float = 95.0,
    min_leverage: int = 1,
) -> int:
    """Map signal confidence to leverage, linearly between two anchors.

    ``confidence <= floor_confidence`` -> ``min_leverage`` (lowest conviction),
    ``confidence >= full_confidence``  -> ``max_leverage`` (full conviction),
    interpolated in between. The result is always within
    ``[min_leverage, max_leverage]`` — never above the configured cap. Uses
    round-half-up so the midpoint rounds toward more leverage.
    """
    max_lev = max(1, int(max_leverage))
    min_lev = max(1, min(int(min_leverage), max_lev))
    span = max(1e-9, float(full_confidence) - float(floor_confidence))
    frac = (float(confidence) - float(floor_confidence)) / span
    frac = max(0.0, min(1.0, frac))
    lev = min_lev + frac * (max_lev - min_lev)
    return int(math.floor(lev + 0.5))


def evaluate(
    *,
    enabled: bool,
    symbol: str,
    side: str,
    confidence: float,
    open_positions: int,
    available_margin: float,
    # config
    max_positions: int,
    max_leverage: int,
    risk_per_trade_pct: float,
    allowed_coins: str,
    allowed_exchanges: str,
    min_confidence: float,
    # leverage sizing
    leverage_scaling: bool = True,
    leverage_floor_confidence: float = 75.0,
    leverage_full_confidence: float = 95.0,
    min_leverage: int = 1,
    # demo context
    has_connected_exchange: bool = True,
) -> RiskDecision:
    """Decide whether to auto-open a paper position for `signal`."""
    if not enabled:
        return RiskDecision(False, "auto-trade disabled")

    if min_confidence > 0 and confidence < min_confidence:
        return RiskDecision(False, f"confidence {confidence:.0f} < min {min_confidence:.0f}")

    coins = _csv_set(allowed_coins)
    if coins and base_coin(symbol) not in coins:
        return RiskDecision(False, f"{base_coin(symbol)} not in allowed coins")

    # allowed_exchanges is enforced for LIVE (20F+). In demo it only gates if the
    # user explicitly restricts exchanges AND has none connected from that list.
    exchanges = _csv_set(allowed_exchanges)
    if exchanges and not has_connected_exchange:
        return RiskDecision(False, "no connected exchange in allowed list")

    if open_positions >= max_positions:
        return RiskDecision(False, f"max positions reached ({open_positions}/{max_positions})")

    if available_margin <= 0:
        return RiskDecision(False, "no available margin")

    if leverage_scaling:
        leverage = scale_leverage(
            confidence,
            max_leverage,
            floor_confidence=leverage_floor_confidence,
            full_confidence=leverage_full_confidence,
            min_leverage=min_leverage,
        )
    else:
        leverage = max(1, int(max_leverage))
    return RiskDecision(True, "ok", leverage=leverage, risk_pct=risk_per_trade_pct)


def trailing_stop(side: str, reference_price: float, distance_pct: float) -> float:
    """New protective stop trailing `distance_pct` behind `reference_price`."""
    frac = distance_pct / 100.0
    if side == pmath.LONG:
        return reference_price * (1.0 - frac)
    return reference_price * (1.0 + frac)


def tighten_stop(side: str, current_stop: float, candidate_stop: float) -> float:
    """Only ever move a stop in the favourable (risk-reducing) direction."""
    if current_stop <= 0:
        return candidate_stop
    if side == pmath.LONG:
        return max(current_stop, candidate_stop)
    return min(current_stop, candidate_stop)
