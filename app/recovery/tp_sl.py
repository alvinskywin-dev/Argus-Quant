"""
Sprint 21C — TP/SL synchronisation status (pure logic).

A live position is "protected" when its stop-loss (and, where intended, its
take-profit) order is actually working on the exchange. After a restart, crash,
or partial failure those orders may be gone while the position is still open —
the most dangerous live-trading state. This module computes the protection
status; the engine acts on it (retry placement or mark UNSAFE).
"""
from __future__ import annotations

# tp_sl_status values
SYNCED = "SYNCED"
MISSING_SL = "MISSING_SL"
MISSING_TP = "MISSING_TP"
MISSING_BOTH = "MISSING_BOTH"
UNKNOWN = "UNKNOWN"
UNSAFE = "UNSAFE"

ALL_STATUSES = (SYNCED, MISSING_SL, MISSING_TP, MISSING_BOTH, UNKNOWN, UNSAFE)


def compute_tp_sl_status(
    has_tp: bool, has_sl: bool, *, expected_tp: bool = True, expected_sl: bool = True,
) -> str:
    """
    Compare working protective orders against what the position expects.

    ``expected_tp`` / ``expected_sl`` reflect whether the position was opened
    with a TP / SL target (a position with no SL target cannot be "missing" one).
    """
    need_tp = expected_tp and not has_tp
    need_sl = expected_sl and not has_sl
    if need_tp and need_sl:
        return MISSING_BOTH
    if need_sl:
        return MISSING_SL
    if need_tp:
        return MISSING_TP
    return SYNCED


def is_protected(status: str) -> bool:
    """True only when nothing protective is missing."""
    return status == SYNCED


def is_unsafe_status(status: str) -> bool:
    """A position with a missing stop-loss (or explicitly UNSAFE) is unsafe."""
    return status in (MISSING_SL, MISSING_BOTH, UNSAFE)
