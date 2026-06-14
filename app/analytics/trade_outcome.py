"""
Lifecycle-aware trade outcome classification.

The bug this module fixes: a trade that hits TP1/TP2/TP3 and then drifts back to
the stop loss used to be recorded as a plain ``SL`` (a loss), because only the
*latest* status was inspected. Winrate was understated as a result.

Outcome is derived from the trade's *lifecycle* (which take-profit levels were
ever reached) rather than its last status alone. The lifecycle is read from
``diagnostics.tp_history`` (persisted by the tracker) and degrades gracefully to
the latest ``status`` for historical signals that predate the tp_history field.

Pure functions only — no DB, no I/O. Safe to import anywhere.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# ── Outcome states ────────────────────────────────────────────────────────────
OUTCOME_LOSS = "LOSS"
OUTCOME_PARTIAL_WIN = "PARTIAL_WIN"
OUTCOME_WIN = "WIN"
OUTCOME_FULL_WIN = "FULL_WIN"
OUTCOME_BREAKEVEN = "BREAKEVEN"
OUTCOME_OPEN = "OPEN"

# ── Winrate buckets (how an outcome counts toward winrate) ────────────────────
BUCKET_WIN = "WIN"
BUCKET_LOSS = "LOSS"
BUCKET_BREAKEVEN = "BREAKEVEN"
BUCKET_OPEN = "OPEN"

_WINRATE_BUCKET = {
    OUTCOME_FULL_WIN: BUCKET_WIN,
    OUTCOME_WIN: BUCKET_WIN,
    OUTCOME_PARTIAL_WIN: BUCKET_WIN,
    OUTCOME_BREAKEVEN: BUCKET_BREAKEVEN,
    OUTCOME_LOSS: BUCKET_LOSS,
    OUTCOME_OPEN: BUCKET_OPEN,
}

_OPEN_STATUSES = {"OPEN", "ACTIVE", "PENDING", ""}
_BE_STATUSES = {"BE", "BREAKEVEN", "BREAK_EVEN"}
_TP_RANK = {"TP1": 1, "TP2": 2, "TP3": 3}


@dataclass
class TradeOutcome:
    outcome: str  # LOSS | PARTIAL_WIN | WIN | FULL_WIN | BREAKEVEN | OPEN
    winrate_bucket: str  # WIN | LOSS | BREAKEVEN | OPEN
    max_tp_hit: int  # 0..3 — highest take-profit ever reached
    first_exit_event: Optional[str]
    final_exit_event: Optional[str]
    realized_pnl: Optional[float]
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _coerce_diag(diagnostics: Any) -> dict:
    """Accept a dict or JSON-string diagnostics blob; return a dict (best-effort)."""
    if isinstance(diagnostics, dict):
        return diagnostics
    if isinstance(diagnostics, str) and diagnostics:
        try:
            parsed = json.loads(diagnostics)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _to_iso(value: Any) -> Optional[str]:
    """Normalise a datetime (or pass-through string) to an ISO-8601 UTC string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return None


def classify_trade_outcome(
    status: Optional[str],
    tp1_hit_at: Any = None,
    tp2_hit_at: Any = None,
    tp3_hit_at: Any = None,
    sl_hit_at: Any = None,
    realized_pnl: Optional[float] = None,
    diagnostics: Any = None,
) -> TradeOutcome:
    """Classify a trade into a lifecycle-aware :class:`TradeOutcome`.

    Precedence (highest take-profit ever reached wins, even if price later
    returned to the stop):

      * status OPEN/ACTIVE/PENDING with no exits -> OPEN
      * TP3 ever hit                              -> FULL_WIN
      * TP2 ever hit                              -> WIN
      * TP1 ever hit                              -> PARTIAL_WIN
      * SL hit and no TP ever hit                 -> LOSS
      * explicit break-even                       -> BREAKEVEN
      * otherwise (manual/closed)                 -> realized_pnl sign:
            > 0 WIN, == 0 BREAKEVEN, < 0 LOSS
    """
    status_u = (status or "").upper()
    diag = _coerce_diag(diagnostics)
    hist = diag.get("tp_history")
    hist = hist if isinstance(hist, dict) else {}

    # Backfill explicit args from persisted tp_history when not supplied.
    tp1_hit_at = tp1_hit_at or hist.get("tp1_hit_at")
    tp2_hit_at = tp2_hit_at or hist.get("tp2_hit_at")
    tp3_hit_at = tp3_hit_at or hist.get("tp3_hit_at")
    sl_hit_at = sl_hit_at or hist.get("sl_hit_at")
    if realized_pnl is None:
        realized_pnl = hist.get("realized_pnl")

    first_exit = hist.get("first_exit_event")
    final_exit = hist.get("final_exit_event") or (status_u or None)

    # Highest take-profit ever reached — from timestamps, status, and history.
    max_tp = 0
    if tp1_hit_at:
        max_tp = max(max_tp, 1)
    if tp2_hit_at:
        max_tp = max(max_tp, 2)
    if tp3_hit_at:
        max_tp = max(max_tp, 3)
    max_tp = max(max_tp, _TP_RANK.get(status_u, 0))
    if isinstance(hist.get("max_tp_hit"), int):
        max_tp = max(max_tp, hist["max_tp_hit"])

    sl_hit = bool(sl_hit_at) or status_u == "SL"
    is_be = status_u in _BE_STATUSES

    def _mk(outcome: str, reason: str) -> TradeOutcome:
        return TradeOutcome(
            outcome=outcome,
            winrate_bucket=_WINRATE_BUCKET[outcome],
            max_tp_hit=max_tp,
            first_exit_event=first_exit,
            final_exit_event=final_exit,
            realized_pnl=(round(float(realized_pnl), 4) if realized_pnl is not None else None),
            reason=reason,
        )

    # Still open — no take-profit, no stop, not an explicit break-even close.
    if status_u in _OPEN_STATUSES and max_tp == 0 and not sl_hit and not is_be:
        return _mk(OUTCOME_OPEN, "trade still open")

    # Lifecycle wins take precedence over a later stop-out.
    if max_tp >= 3:
        return _mk(OUTCOME_FULL_WIN, "TP3 reached")
    if max_tp == 2:
        return _mk(OUTCOME_WIN, "TP2 reached before exit")
    if max_tp == 1:
        return _mk(OUTCOME_PARTIAL_WIN, "TP1 reached before exit")

    # No take-profit ever reached.
    if sl_hit and not is_be:
        return _mk(OUTCOME_LOSS, "stop loss hit before any take-profit")
    if is_be:
        return _mk(OUTCOME_BREAKEVEN, "break-even close, no take-profit")

    # Manual / generic close with no TP/SL signal — fall back to realized PnL.
    if realized_pnl is not None:
        if realized_pnl > 0:
            return _mk(OUTCOME_WIN, "manual close in profit")
        if realized_pnl < 0:
            return _mk(OUTCOME_LOSS, "manual close at a loss")
        return _mk(OUTCOME_BREAKEVEN, "manual close flat")

    # No exit information at all — treat as still open (excluded from winrate).
    return _mk(OUTCOME_OPEN, "no exit information")


# ── tp_history persistence helper (used by the tracker) ───────────────────────
def record_exit_event(
    diagnostics: Any,
    event: str,
    event_time: Any = None,
    realized_pnl: Optional[float] = None,
) -> dict:
    """Merge a TP/SL/BE *event* into the diagnostics ``tp_history`` and return the
    updated diagnostics dict (caller is responsible for JSON-serialising it).

    Idempotent per level: the first timestamp for each level is preserved. This
    is what makes a TP1-then-SL trade keep its ``max_tp_hit = 1`` even though the
    latest status becomes ``SL``.
    """
    diag = dict(_coerce_diag(diagnostics))
    hist = diag.get("tp_history")
    hist = dict(hist) if isinstance(hist, dict) else {}

    iso = _to_iso(event_time) or _to_iso(datetime.now(timezone.utc))
    ev = (event or "").upper()

    # A direct jump (e.g. price gaps straight to TP2) implies the lower levels
    # were crossed too — record them so max_tp_hit stays coherent.
    if ev in ("TP1", "TP2", "TP3") and not hist.get("tp1_hit_at"):
        hist["tp1_hit_at"] = iso
    if ev in ("TP2", "TP3") and not hist.get("tp2_hit_at"):
        hist["tp2_hit_at"] = iso
    if ev == "TP3" and not hist.get("tp3_hit_at"):
        hist["tp3_hit_at"] = iso
    if ev == "SL":
        hist["sl_hit_at"] = iso
    if ev in _BE_STATUSES:
        hist["be_hit_at"] = iso

    if not hist.get("first_exit_event"):
        hist["first_exit_event"] = ev
    hist["final_exit_event"] = ev
    hist["max_tp_hit"] = max(int(hist.get("max_tp_hit") or 0), _TP_RANK.get(ev, 0))
    if realized_pnl is not None:
        hist["realized_pnl"] = round(float(realized_pnl), 4)

    outcome = classify_trade_outcome(
        status=ev,
        diagnostics={"tp_history": hist},
        realized_pnl=realized_pnl,
    )
    hist["trade_outcome"] = outcome.outcome

    diag["tp_history"] = hist
    return diag


# ── Convenience helpers for ORM Signal objects / dicts ────────────────────────
def _attr(sig: Any, key: str) -> Any:
    if isinstance(sig, dict):
        return sig.get(key)
    return getattr(sig, key, None)


def outcome_for_signal(sig: Any) -> TradeOutcome:
    """Classify a Signal ORM row (or signal dict) using its status + diagnostics."""
    return classify_trade_outcome(
        status=_attr(sig, "status"),
        realized_pnl=_attr(sig, "pnl_pct"),
        diagnostics=_attr(sig, "diagnostics"),
    )


def winrate_bucket_for_signal(sig: Any) -> str:
    """Return WIN / LOSS / BREAKEVEN / OPEN for a signal."""
    return outcome_for_signal(sig).winrate_bucket


def is_win(sig: Any) -> bool:
    return winrate_bucket_for_signal(sig) == BUCKET_WIN


def is_loss(sig: Any) -> bool:
    return winrate_bucket_for_signal(sig) == BUCKET_LOSS


def is_breakeven(sig: Any) -> bool:
    return winrate_bucket_for_signal(sig) == BUCKET_BREAKEVEN


# ── Display helpers (dashboard badges / telegram) ─────────────────────────────
_OUTCOME_LABEL = {
    OUTCOME_FULL_WIN: "FULL WIN",
    OUTCOME_WIN: "WIN",
    OUTCOME_PARTIAL_WIN: "PARTIAL WIN",
    OUTCOME_BREAKEVEN: "BREAKEVEN",
    OUTCOME_LOSS: "LOSS",
    OUTCOME_OPEN: "OPEN",
}


def outcome_label(outcome: str) -> str:
    """Human-readable badge text for an outcome state."""
    return _OUTCOME_LABEL.get((outcome or "").upper(), outcome or "")
