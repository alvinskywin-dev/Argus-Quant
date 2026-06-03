"""
Sprint 22D — Break-Even + Partial TP Engine.

When TP1 is hit the engine protects the trade: close a configurable slice
(default 40%), move the stop to entry (break-even), and arm a trailing stop on
the remainder. It is a **planner**, not an executor — `plan_tp1_actions()`
returns a list of *intents* (reduce-only close, SL move, trailing arm). The
caller (the existing live/paper execution layer) is responsible for turning an
intent into an order, which keeps reconciliation, recovery and the live safety
gate fully in charge.

Hard invariants enforced here, regardless of config:
  • SL is NEVER widened (a LONG break-even/trailing stop only moves up).
  • Partial closes are always reduce-only and never exceed the open size.
  • No intent is produced more than once (idempotent on the passed-in state).
  • With BREAK_EVEN_ENGINE_ENABLED=false, no intents are produced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional

from app.config import settings


@dataclass
class BreakEvenIntent:
    """A single reduce-only / protective action to be executed elsewhere."""

    action: str  # PARTIAL_CLOSE | MOVE_SL | ARM_TRAILING | UPDATE_TRAILING
    symbol: str
    side: str  # position side: LONG / SHORT
    reduce_only: bool = True
    quantity: Optional[float] = None  # for PARTIAL_CLOSE
    new_stop_price: Optional[float] = None  # for MOVE_SL / UPDATE_TRAILING
    trailing_distance_percent: Optional[float] = None  # for ARM_TRAILING
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BreakEvenDiagnostics:
    partial_tp_executed: bool = False
    break_even_activated: bool = False
    trailing_stop_active: bool = False
    realized_partial_pnl: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PositionProtectionState:
    """Mutable per-position flags the caller persists between ticks so actions
    are not duplicated."""

    partial_tp_done: bool = False
    break_even_done: bool = False
    trailing_active: bool = False
    trailing_stop_price: Optional[float] = None
    realized_partial_pnl: float = 0.0


def _is_long(side: str) -> bool:
    return (side or "").upper() == "LONG"


def plan_tp1_actions(
    *,
    symbol: str,
    side: str,
    entry_price: float,
    open_quantity: float,
    current_stop: Optional[float],
    tp1_price: float,
    state: PositionProtectionState,
) -> List[BreakEvenIntent]:
    """Return the protective intents to run when TP1 has just been hit.

    Idempotent: actions already recorded in ``state`` are not re-emitted. The
    caller must mark them done (via :func:`apply_intents`) once executed.
    """
    if not settings.break_even_engine_enabled:
        return []
    if open_quantity <= 0 or entry_price <= 0:
        return []

    intents: List[BreakEvenIntent] = []
    long = _is_long(side)

    # 1) Partial close (reduce-only).
    if not state.partial_tp_done:
        pct = max(0.0, min(100.0, settings.partial_tp_percent)) / 100.0
        qty = round(open_quantity * pct, 10)
        if qty > 0:
            realized = (tp1_price - entry_price) * qty * (1 if long else -1)
            intents.append(
                BreakEvenIntent(
                    action="PARTIAL_CLOSE",
                    symbol=symbol,
                    side=side,
                    reduce_only=True,
                    quantity=qty,
                    reason=f"TP1 hit — take {settings.partial_tp_percent:.0f}% off",
                )
            )
            # realised PnL is informational; caller confirms on fill.
            intents[-1].new_stop_price = None
            state.realized_partial_pnl = round(realized, 8)

    # 2) Move SL to entry (break-even) — never widening.
    if settings.move_sl_to_entry_on_tp1 and not state.break_even_done:
        if _is_tighter(long, current_stop, entry_price):
            intents.append(
                BreakEvenIntent(
                    action="MOVE_SL",
                    symbol=symbol,
                    side=side,
                    reduce_only=True,
                    new_stop_price=entry_price,
                    reason="Break-even: move SL to entry after TP1",
                )
            )

    # 3) Arm trailing stop on the remainder.
    if settings.trailing_stop_enabled and not state.trailing_active:
        dist = max(0.0, settings.trailing_stop_distance_percent)
        if dist > 0:
            trail_price = _trail_from(long, tp1_price, dist)
            # Only arm if it is tighter than break-even/current stop.
            ref = entry_price if settings.move_sl_to_entry_on_tp1 else current_stop
            if _is_tighter(long, ref, trail_price):
                intents.append(
                    BreakEvenIntent(
                        action="ARM_TRAILING",
                        symbol=symbol,
                        side=side,
                        reduce_only=True,
                        new_stop_price=trail_price,
                        trailing_distance_percent=dist,
                        reason=f"Arm {dist:.2f}% trailing stop on remainder",
                    )
                )
    return intents


def update_trailing_stop(
    *,
    symbol: str,
    side: str,
    last_price: float,
    state: PositionProtectionState,
) -> Optional[BreakEvenIntent]:
    """Once trailing is active, ratchet the stop in the favourable direction
    only. Returns an UPDATE_TRAILING intent or None if no tightening is due."""
    if not settings.break_even_engine_enabled or not settings.trailing_stop_enabled:
        return None
    if not state.trailing_active or last_price <= 0:
        return None
    dist = max(0.0, settings.trailing_stop_distance_percent)
    if dist <= 0:
        return None
    long = _is_long(side)
    candidate = _trail_from(long, last_price, dist)
    if _is_tighter(long, state.trailing_stop_price, candidate):
        return BreakEvenIntent(
            action="UPDATE_TRAILING",
            symbol=symbol,
            side=side,
            reduce_only=True,
            new_stop_price=candidate,
            trailing_distance_percent=dist,
            reason="Ratchet trailing stop",
        )
    return None


def apply_intents(
    state: PositionProtectionState, intents: List[BreakEvenIntent]
) -> PositionProtectionState:
    """Fold executed intents back into the state (caller calls after success)."""
    for it in intents:
        if it.action == "PARTIAL_CLOSE":
            state.partial_tp_done = True
        elif it.action == "MOVE_SL":
            state.break_even_done = True
            state.trailing_stop_price = it.new_stop_price
        elif it.action == "ARM_TRAILING":
            state.trailing_active = True
            state.trailing_stop_price = it.new_stop_price
        elif it.action == "UPDATE_TRAILING":
            state.trailing_stop_price = it.new_stop_price
    return state


def diagnostics(state: PositionProtectionState) -> BreakEvenDiagnostics:
    return BreakEvenDiagnostics(
        partial_tp_executed=state.partial_tp_done,
        break_even_activated=state.break_even_done,
        trailing_stop_active=state.trailing_active,
        realized_partial_pnl=round(state.realized_partial_pnl, 8),
    )


def _trail_from(long: bool, price: float, dist_pct: float) -> float:
    """Stop ``dist_pct`` behind ``price`` in the protective direction."""
    if long:
        return round(price * (1 - dist_pct / 100.0), 10)
    return round(price * (1 + dist_pct / 100.0), 10)


def _is_tighter(long: bool, current: Optional[float], candidate: Optional[float]) -> bool:
    """True if ``candidate`` is a strictly *tighter* (never wider) stop.

    LONG: tighter == higher. SHORT: tighter == lower. A None current stop is
    always improved upon by any candidate.
    """
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate > current if long else candidate < current
