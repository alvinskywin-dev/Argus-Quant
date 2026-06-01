"""
Sprint 21E — net PnL accounting math (pure).

Net PnL is the truth of live trading: gross price PnL minus commission, funding,
and slippage. When a component cannot be measured exactly (e.g. funding not yet
fetched from the exchange), the breakdown is flagged PARTIAL so dashboards never
present an estimate as if it were settled.
"""
from __future__ import annotations

from dataclasses import dataclass

# Conservative default Binance USDT-M taker fee (0.04%); used only for estimates.
DEFAULT_TAKER_RATE = 0.0004

# estimate_quality values
EXACT = "EXACT"        # every component came from settled exchange data
PARTIAL = "PARTIAL"    # at least one component is an estimate
ESTIMATED = "ESTIMATED"  # gross is real but all costs are estimated


@dataclass
class PnlBreakdown:
    gross_pnl: float
    commission: float
    funding_fee: float
    slippage: float
    net_pnl: float
    net_roe: float
    total_fees: float
    estimate_quality: str


def estimate_commission(notional: float, *, rate: float = DEFAULT_TAKER_RATE,
                        round_trip: bool = True) -> float:
    """Estimated commission for a notional. round_trip charges entry + exit."""
    legs = 2 if round_trip else 1
    return abs(notional) * rate * legs


def slippage_cost(expected_price: float, fill_price: float, qty: float) -> float:
    """Absolute cost of the difference between expected and actual fill price."""
    if expected_price <= 0 or fill_price <= 0 or qty <= 0:
        return 0.0
    return abs(fill_price - expected_price) * qty


def compute_net_pnl(
    gross_pnl: float,
    *,
    commission: float = 0.0,
    funding_fee: float = 0.0,
    slippage: float = 0.0,
    margin: float | None = None,
    commission_known: bool = False,
    funding_known: bool = False,
    slippage_known: bool = False,
) -> PnlBreakdown:
    """
    net_pnl = gross_pnl - commission - funding_fee - slippage.

    The *_known flags mark whether each cost came from settled exchange data
    (True) or is an estimate (False); estimate_quality summarises them.

    Funding convention: ``funding_fee`` > 0 means funding PAID (a cost that
    reduces net); < 0 means funding RECEIVED (increases net).
    """
    commission = abs(commission)
    slippage = abs(slippage)
    total_fees = commission + slippage + abs(funding_fee)
    net_pnl = gross_pnl - commission - funding_fee - slippage
    net_roe = (net_pnl / margin * 100.0) if (margin and margin > 0) else 0.0

    if commission_known and funding_known and slippage_known:
        quality = EXACT
    elif not commission_known and not funding_known and not slippage_known:
        quality = ESTIMATED
    else:
        quality = PARTIAL

    return PnlBreakdown(
        gross_pnl=round(gross_pnl, 8), commission=round(commission, 8),
        funding_fee=round(funding_fee, 8), slippage=round(slippage, 8),
        net_pnl=round(net_pnl, 8), net_roe=round(net_roe, 4),
        total_fees=round(total_fees, 8), estimate_quality=quality)


def holding_seconds(opened_at, closed_at) -> int:
    """Whole seconds a position was held; 0 if either timestamp is missing."""
    if not opened_at or not closed_at:
        return 0
    delta = (closed_at - opened_at).total_seconds()
    return int(delta) if delta > 0 else 0
