"""
Slippage guard maths (live-safety #4).

Pure functions only — no DB, no network, fully unit-testable. The live execution
service uses these to (a) refuse a MARKET entry that would chase a market which
has already moved too far from the intended entry, and (b) record realised
slippage on the fill for review.

Sign convention: positive bps = *adverse* (worse for the trader). For a LONG/BUY
that means a higher price; for a SHORT/SELL a lower price. Favourable moves are
negative and never trip the guard.
"""

from __future__ import annotations

_LONG_SIDES = {"LONG", "BUY"}


def _is_long(side: str) -> bool:
    return str(side or "").upper() in _LONG_SIDES


def slippage_bps(side: str, reference_price: float, fill_price: float) -> float:
    """Signed adverse slippage of ``fill_price`` vs ``reference_price``, in bps.

    Returns 0.0 when inputs are unusable (non-positive reference).
    """
    try:
        ref = float(reference_price)
        fill = float(fill_price)
    except (TypeError, ValueError):
        return 0.0
    if ref <= 0:
        return 0.0
    raw = (fill - ref) / ref * 1e4
    return raw if _is_long(side) else -raw


def exceeds_slippage(side: str, reference_price: float, fill_price: float, max_bps: float) -> bool:
    """True when adverse slippage exceeds ``max_bps`` (<= 0 disables the guard)."""
    if not max_bps or max_bps <= 0:
        return False
    return slippage_bps(side, reference_price, fill_price) > max_bps
