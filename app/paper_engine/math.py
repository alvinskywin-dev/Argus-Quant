"""
Sprint 20B — paper futures math.

Pure functions only (no DB, no I/O). Isolated-margin model, USDT-margined
linear perpetual. All inputs are plain floats so the maths can be unit-tested
in isolation and reused by the auto-trading engine in later sprints.
"""

from __future__ import annotations

LONG = "LONG"
SHORT = "SHORT"

# Default maintenance-margin rate used for liquidation estimates.
DEFAULT_MMR = 0.005  # 0.5%


def position_quantity(notional_usdt: float, entry_price: float) -> float:
    """Base-asset units for a position of `notional_usdt` value at `entry_price`."""
    if entry_price <= 0:
        return 0.0
    return notional_usdt / entry_price


def required_margin(notional_usdt: float, leverage: float) -> float:
    """Isolated margin required to open `notional_usdt` at `leverage`."""
    if leverage <= 0:
        return notional_usdt
    return notional_usdt / leverage


def liquidation_price(
    side: str, entry_price: float, leverage: float, mmr: float = DEFAULT_MMR
) -> float:
    """
    Estimated isolated-margin liquidation price.

    LONG  liquidates when price falls to entry * (1 - 1/L + mmr)
    SHORT liquidates when price rises to entry * (1 + 1/L - mmr)
    """
    if entry_price <= 0 or leverage <= 0:
        return 0.0
    inv = 1.0 / leverage
    if side == LONG:
        return max(0.0, entry_price * (1.0 - inv + mmr))
    return entry_price * (1.0 + inv - mmr)


def unrealized_pnl(side: str, entry_price: float, mark_price: float, notional_usdt: float) -> float:
    """PnL in USDT if the position were marked at `mark_price`."""
    if entry_price <= 0:
        return 0.0
    move = mark_price / entry_price - 1.0
    return notional_usdt * move if side == LONG else notional_usdt * -move


def price_move_pct(side: str, entry_price: float, exit_price: float) -> float:
    """Signed price move in percent, in the position's favour direction."""
    if entry_price <= 0:
        return 0.0
    move = (exit_price / entry_price - 1.0) * 100.0
    return move if side == LONG else -move


def roe_pct(pnl_usdt: float, margin_usdt: float) -> float:
    """Return on equity (margin) in percent."""
    if margin_usdt <= 0:
        return 0.0
    return pnl_usdt / margin_usdt * 100.0


def funding_cost(side: str, notional_usdt: float, funding_rate: float, intervals: int) -> float:
    """
    Funding paid (positive) or received (negative) over `intervals` funding
    windows. With a positive funding rate, longs pay and shorts receive.
    """
    base = notional_usdt * funding_rate * max(0, intervals)
    return base if side == LONG else -base


def is_liquidated(side: str, mark_price: float, liq_price: float) -> bool:
    if liq_price <= 0:
        return False
    return mark_price <= liq_price if side == LONG else mark_price >= liq_price


def risk_based_notional(
    balance: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    *,
    leverage: float,
    max_notional_frac: float = 1.0,
) -> float:
    """
    Notional that risks `risk_pct`% of balance between entry and stop_loss,
    capped so the required margin never exceeds `max_notional_frac` of balance.
    """
    if entry_price <= 0 or balance <= 0:
        return 0.0
    risk_usdt = balance * (risk_pct / 100.0)
    dist = abs(entry_price - stop_loss) / entry_price if stop_loss > 0 else 0.0
    notional = (risk_usdt / dist) if dist > 0 else risk_usdt
    # Cap by margin availability: margin = notional / leverage <= cap
    margin_cap = balance * max_notional_frac
    max_notional = margin_cap * max(1.0, leverage)
    return max(0.0, min(notional, max_notional))
