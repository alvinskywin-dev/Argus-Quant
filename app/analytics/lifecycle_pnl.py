"""
Lifecycle-aware realized PnL for the signal tracker.

The bug this fixes: when a trade reaches a take-profit and *then* drifts back to
the stop, the tracker overwrote ``pnl_pct`` with the full stop-loss number — so a
trade the win-rate counts as a win (TP reached) was booked as a −6% loss. PnL and
win-rate therefore disagreed.

This module models a realistic partial-booking lifecycle:

  * a fraction is realized at TP1, another fraction at TP2, the remainder rides
    to TP3;
  * after TP1 the stop is moved to break-even, so the un-booked remainder exits
    at 0% on a later stop-out instead of the full stop loss.

The result is a *blended* realized PnL: booked partials + the remainder's exit.
Pure functions only — no DB, no I/O.
"""

from __future__ import annotations


def price_pct(side: str, entry: float, price: float) -> float:
    """Signed % move from *entry* to *price* for the given side."""
    if entry <= 0:
        return 0.0
    if (side or "").upper() == "LONG":
        return (price - entry) / entry * 100.0
    return (entry - price) / entry * 100.0


def blended_realized_pnl(
    *,
    side: str,
    entry: float,
    tp1: float,
    tp2: float,
    tp3: float,
    stop_loss: float,
    max_tp_hit: int,
    final_event: str,
    tp1_frac: float,
    tp2_frac: float,
    sl_to_breakeven_after_tp1: bool,
) -> float:
    """Return the blended realized PnL% for a finished trade lifecycle.

    ``max_tp_hit`` is the highest take-profit ever reached (0..3) and
    ``final_event`` is the terminal event (``"TP3"`` or ``"SL"``).

    Booking model (fractions clamped to a sane [0,1] split):

      * TP1 reached  → book ``tp1_frac`` at the TP1 price
      * TP2 reached  → additionally book ``tp2_frac`` at the TP2 price
      * TP3 (final)  → book the remainder at the TP3 price
      * SL (final)   → the still-open remainder exits at break-even (0%) when a
                       TP was already hit and SL-to-BE is on, otherwise at the
                       full stop loss.
    """
    f1 = max(0.0, min(1.0, float(tp1_frac)))
    f2 = max(0.0, min(1.0 - f1, float(tp2_frac)))
    rem = max(0.0, 1.0 - f1 - f2)

    booked = 0.0
    if max_tp_hit >= 1:
        booked += f1 * price_pct(side, entry, tp1)
    if max_tp_hit >= 2:
        booked += f2 * price_pct(side, entry, tp2)

    ev = (final_event or "").upper()

    if ev == "TP3" or max_tp_hit >= 3:
        return round(booked + rem * price_pct(side, entry, tp3), 3)

    if ev == "SL":
        # Fraction of the position still open when the stop was hit.
        open_frac = 1.0 - (f1 if max_tp_hit >= 1 else 0.0) - (f2 if max_tp_hit >= 2 else 0.0)
        open_frac = max(0.0, open_frac)
        if max_tp_hit >= 1 and sl_to_breakeven_after_tp1:
            exit_pct = 0.0  # stop moved to break-even after TP1
        else:
            exit_pct = price_pct(side, entry, stop_loss)  # full stop (no TP yet)
        return round(booked + open_frac * exit_pct, 3)

    # Non-terminal fallback — should not be reached for finished trades.
    return round(booked, 3)
