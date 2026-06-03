"""
Sprint 20E — pure safety-rule maths (no DB, no I/O).

Decisions only, so every branch is unit-testable. DB aggregation and state
mutation live in app.safety.service.
"""

from __future__ import annotations

# Correlation clusters — positions in the same cluster + same side count as
# correlated for the max-correlated-positions cap. Anything unlisted is "ALT".
_CLUSTERS: dict[str, str] = {
    "BTC": "MAJOR",
    "ETH": "MAJOR",
    "SOL": "L1",
    "AVAX": "L1",
    "ADA": "L1",
    "DOT": "L1",
    "NEAR": "L1",
    "BNB": "L1",
    "APT": "L1",
    "SUI": "L1",
    "DOGE": "MEME",
    "SHIB": "MEME",
    "PEPE": "MEME",
    "WIF": "MEME",
    "BONK": "MEME",
    "1000PEPE": "MEME",
    "1000SHIB": "MEME",
}


def base_coin(symbol: str) -> str:
    s = symbol.upper()
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def correlation_cluster(symbol: str) -> str:
    return _CLUSTERS.get(base_coin(symbol), "ALT")


def consecutive_losses(pnls: list[float]) -> int:
    """Count leading losing trades in a most-recent-first list of PnLs."""
    n = 0
    for pnl in pnls:
        if pnl < 0:
            n += 1
        else:
            break
    return n


def loss_exceeds_limit(realized_pnl: float, initial_balance: float, max_loss_pct: float) -> bool:
    """True if the (negative) realized PnL breaches `max_loss_pct` of balance."""
    if initial_balance <= 0 or max_loss_pct <= 0:
        return False
    return realized_pnl <= -(initial_balance * max_loss_pct / 100.0)


def count_correlated(
    open_clusters_sides: list[tuple[str, str]], new_cluster: str, new_side: str
) -> int:
    """How many existing open positions are in the same cluster AND same side."""
    return sum(1 for cl, side in open_clusters_sides if cl == new_cluster and side == new_side)
