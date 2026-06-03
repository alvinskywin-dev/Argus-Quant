"""
Sprint 22A — Portfolio Exposure + Position Lock Engine.

Prevents overexposure, correlated stacking, and portfolio-level direction risk
*before* a signal becomes a (paper or live) position. The system can otherwise
open BTC/ETH/SOL/DOGE all LONG at once — effectively one large correlated bet.

The engine is pure and self-contained: it takes a snapshot of the current
open positions, pending orders, and the day's realised PnL, and answers
``can_open_position()``. It places NO orders and reads NO globals beyond
``settings``. With ``PORTFOLIO_EXPOSURE_ENGINE_ENABLED=false`` it always
allows (back-compat), but still computes diagnostics so the API/UI can show the
exposure picture in shadow.

Nothing here removes a safety system: it is an *additional* gate that can only
reject, never force, an entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from app.config import _norm_base_symbol, settings


@dataclass
class Position:
    """A minimal view of an open position / pending order."""

    symbol: str
    side: str  # LONG / SHORT
    notional: float = 0.0
    status: str = "OPEN"  # OPEN | PENDING

    @property
    def base(self) -> str:
        return _norm_base_symbol(self.symbol)

    @property
    def dir(self) -> str:
        return (self.side or "").upper()


@dataclass
class PortfolioExposureState:
    """Computed snapshot of a user's portfolio exposure."""

    open_positions: List[Position] = field(default_factory=list)
    pending_orders: List[Position] = field(default_factory=list)
    correlated_groups: dict = field(default_factory=dict)
    long_count: int = 0
    short_count: int = 0
    exposure_score: float = 0.0
    daily_pnl_percent: float = 0.0
    locked_symbols: List[str] = field(default_factory=list)

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    @property
    def long_short_ratio(self) -> Optional[float]:
        if self.short_count == 0:
            return None if self.long_count == 0 else float("inf")
        return round(self.long_count / self.short_count, 3)

    def to_diagnostics(self) -> dict:
        return {
            "exposure_score": round(self.exposure_score, 2),
            "open_positions": self.open_count,
            "pending_orders": len(self.pending_orders),
            "long_count": self.long_count,
            "short_count": self.short_count,
            "long_short_ratio": (
                None
                if self.long_short_ratio is None
                else (None if self.long_short_ratio == float("inf") else self.long_short_ratio)
            ),
            "daily_loss_percent": round(max(0.0, -self.daily_pnl_percent), 2),
            "daily_pnl_percent": round(self.daily_pnl_percent, 2),
            "correlation_groups": {g: sorted(s) for g, s in self.correlated_groups.items()},
            "locked_symbols": sorted(self.locked_symbols),
        }


@dataclass
class ExposureDecision:
    """Result of ``can_open_position``."""

    allowed: bool
    reason: str
    exposure_score: float = 0.0
    same_direction_count: int = 0
    correlated_group: Optional[str] = None
    correlated_count: int = 0
    daily_loss_percent: float = 0.0
    enabled: bool = True

    def to_diagnostics(self) -> dict:
        return {
            "portfolio_exposure_enabled": self.enabled,
            "portfolio_allowed": self.allowed,
            "portfolio_reject_reason": None if self.allowed else self.reason,
            "exposure_score": round(self.exposure_score, 2),
            "same_direction_count": self.same_direction_count,
            "correlated_group": self.correlated_group,
            "correlated_count": self.correlated_count,
            "daily_loss_percent": round(self.daily_loss_percent, 2),
        }


def _as_positions(items: Optional[Iterable]) -> List[Position]:
    """Coerce dicts / objects / Position into a list of Position."""
    out: List[Position] = []
    for it in items or []:
        if isinstance(it, Position):
            out.append(it)
        elif isinstance(it, dict):
            out.append(
                Position(
                    symbol=str(it.get("symbol", "")),
                    side=str(it.get("side", it.get("direction", ""))),
                    notional=float(it.get("notional", it.get("size", 0.0)) or 0.0),
                    status=str(it.get("status", "OPEN")),
                )
            )
        else:  # duck-typed object
            out.append(
                Position(
                    symbol=str(getattr(it, "symbol", "")),
                    side=str(getattr(it, "side", getattr(it, "direction", ""))),
                    notional=float(getattr(it, "notional", 0.0) or 0.0),
                    status=str(getattr(it, "status", "OPEN")),
                )
            )
    return out


def is_correlated_symbol(symbol: str, other: str) -> bool:
    """True if two symbols share a configured correlation group (or are the
    same base asset)."""
    a, b = _norm_base_symbol(symbol), _norm_base_symbol(other)
    if a == b:
        return True
    for members in settings.correlation_group_map.values():
        if a in members and b in members:
            return True
    return False


def _groups_for(symbol: str) -> List[str]:
    base = _norm_base_symbol(symbol)
    return [g for g, members in settings.correlation_group_map.items() if base in members]


def calculate_exposure_score(positions: Iterable, pending: Optional[Iterable] = None) -> float:
    """A 0-100 portfolio risk score.

    Rewards diversification and direction balance; penalises stacking the same
    direction and clustering inside one correlation group. Pure / deterministic.
    """
    pos = _as_positions(positions)
    pen = _as_positions(pending)
    everything = pos + pen
    if not everything:
        return 0.0

    longs = sum(1 for p in everything if p.dir == "LONG")
    shorts = sum(1 for p in everything if p.dir == "SHORT")
    total = len(everything)

    # Directional imbalance: 0 when balanced, up to 1 when all one side.
    imbalance = abs(longs - shorts) / total

    # Correlation clustering: largest share of one correlated group, same dir.
    cluster = 0.0
    cap = settings.max_correlated_positions or 1
    seen_groups: dict = {}
    for p in everything:
        for g in _groups_for(p.symbol):
            key = (g, p.dir)
            seen_groups[key] = seen_groups.get(key, 0) + 1
    if seen_groups:
        cluster = min(1.0, (max(seen_groups.values()) - 1) / max(1, cap))

    # Concentration relative to the per-user cap.
    concentration = min(1.0, total / max(1, settings.max_open_positions_per_user))

    score = 100.0 * (0.45 * imbalance + 0.35 * cluster + 0.20 * concentration)
    return round(min(100.0, score), 2)


def build_state(
    open_positions: Optional[Iterable] = None,
    pending_orders: Optional[Iterable] = None,
    daily_pnl_percent: float = 0.0,
    locked_symbols: Optional[Iterable[str]] = None,
) -> PortfolioExposureState:
    pos = _as_positions(open_positions)
    pen = _as_positions(pending_orders)
    longs = sum(1 for p in pos if p.dir == "LONG")
    shorts = sum(1 for p in pos if p.dir == "SHORT")

    groups: dict = {}
    for p in pos + pen:
        for g in _groups_for(p.symbol):
            groups.setdefault(g, set()).add(p.base)

    return PortfolioExposureState(
        open_positions=pos,
        pending_orders=pen,
        correlated_groups=groups,
        long_count=longs,
        short_count=shorts,
        exposure_score=calculate_exposure_score(pos, pen),
        daily_pnl_percent=float(daily_pnl_percent or 0.0),
        locked_symbols=list(locked_symbols or []),
    )


def is_symbol_locked(symbol: str, state: PortfolioExposureState) -> bool:
    """A symbol is locked if it already has an OPEN position (symbol lock) or it
    is in the explicit locked list."""
    if not settings.symbol_lock_enabled:
        return _norm_base_symbol(symbol) in {_norm_base_symbol(s) for s in state.locked_symbols}
    base = _norm_base_symbol(symbol)
    if base in {_norm_base_symbol(s) for s in state.locked_symbols}:
        return True
    return any(p.base == base for p in state.open_positions)


def has_pending_order(symbol: str, state: PortfolioExposureState) -> bool:
    if not settings.pending_order_lock_enabled:
        return False
    base = _norm_base_symbol(symbol)
    return any(p.base == base for p in state.pending_orders)


def can_open_position(
    symbol: str,
    side: str,
    *,
    open_positions: Optional[Iterable] = None,
    pending_orders: Optional[Iterable] = None,
    daily_pnl_percent: float = 0.0,
    locked_symbols: Optional[Iterable[str]] = None,
) -> ExposureDecision:
    """Decide whether a new ``symbol``/``side`` entry may be opened.

    Returns an ``ExposureDecision``. When the engine is disabled it always
    allows, but still reports the exposure score for diagnostics.
    """
    state = build_state(open_positions, pending_orders, daily_pnl_percent, locked_symbols)
    direction = (side or "").upper()
    same_dir = sum(1 for p in state.open_positions if p.dir == direction)

    # Correlated count: open positions, same direction, sharing a group.
    corr_group: Optional[str] = None
    corr_count = 0
    for g in _groups_for(symbol):
        cnt = sum(
            1 for p in state.open_positions if p.dir == direction and g in _groups_for(p.symbol)
        )
        if cnt > corr_count:
            corr_count, corr_group = cnt, g

    daily_loss = max(0.0, -state.daily_pnl_percent)

    def deny(reason: str) -> ExposureDecision:
        return ExposureDecision(
            allowed=False,
            reason=reason,
            exposure_score=state.exposure_score,
            same_direction_count=same_dir,
            correlated_group=corr_group,
            correlated_count=corr_count,
            daily_loss_percent=daily_loss,
            enabled=settings.portfolio_exposure_engine_enabled,
        )

    if not settings.portfolio_exposure_engine_enabled:
        return ExposureDecision(
            allowed=True,
            reason="portfolio exposure engine disabled",
            exposure_score=state.exposure_score,
            same_direction_count=same_dir,
            correlated_group=corr_group,
            correlated_count=corr_count,
            daily_loss_percent=daily_loss,
            enabled=False,
        )

    # Rule order: cheapest / most specific first.
    if is_symbol_locked(symbol, state):
        return deny(f"symbol {symbol} already open or locked")
    if has_pending_order(symbol, state):
        return deny(f"symbol {symbol} has a pending order")
    if state.open_count >= settings.max_open_positions_per_user:
        return deny(
            f"max open positions reached ({state.open_count}/"
            f"{settings.max_open_positions_per_user})"
        )
    if same_dir >= settings.max_same_direction_positions:
        return deny(
            f"max same-direction ({direction}) positions reached "
            f"({same_dir}/{settings.max_same_direction_positions})"
        )
    if corr_count >= settings.max_correlated_positions:
        return deny(
            f"max correlated {direction} positions in group {corr_group} reached "
            f"({corr_count}/{settings.max_correlated_positions})"
        )
    if settings.max_daily_loss_percent > 0 and daily_loss >= settings.max_daily_loss_percent:
        return deny(
            f"daily loss limit reached ({daily_loss:.2f}% >= "
            f"{settings.max_daily_loss_percent:.2f}%)"
        )

    return ExposureDecision(
        allowed=True,
        reason="ok",
        exposure_score=state.exposure_score,
        same_direction_count=same_dir,
        correlated_group=corr_group,
        correlated_count=corr_count,
        daily_loss_percent=daily_loss,
        enabled=True,
    )
