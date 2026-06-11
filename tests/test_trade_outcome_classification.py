"""
Lifecycle-aware trade outcome classification.

Pins the winrate fix: trades that reach a take-profit and then return to the
stop loss must count as (partial) wins, not losses. Pure unit tests — no DB.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.analytics.trade_outcome import (
    BUCKET_BREAKEVEN,
    BUCKET_LOSS,
    BUCKET_OPEN,
    BUCKET_WIN,
    classify_trade_outcome,
    outcome_for_signal,
    record_exit_event,
    winrate_bucket_for_signal,
)

ISO = "2026-06-03T10:00:00+00:00"
ISO_LATER = "2026-06-03T12:00:00+00:00"


# ── 1. SL before any TP = LOSS ────────────────────────────────────────────────
def test_sl_before_tp_is_loss():
    oc = classify_trade_outcome(status="SL", sl_hit_at=ISO)
    assert oc.outcome == "LOSS"
    assert oc.winrate_bucket == BUCKET_LOSS
    assert oc.max_tp_hit == 0


# ── 2. TP1 then SL = PARTIAL_WIN, winrate bucket WIN ──────────────────────────
def test_tp1_then_sl_is_partial_win():
    oc = classify_trade_outcome(status="SL", tp1_hit_at=ISO, sl_hit_at=ISO_LATER)
    assert oc.outcome == "PARTIAL_WIN"
    assert oc.winrate_bucket == BUCKET_WIN
    assert oc.max_tp_hit == 1
    assert oc.final_exit_event in ("SL", None)


# ── 3. TP2 then SL = WIN ──────────────────────────────────────────────────────
def test_tp2_then_sl_is_win():
    oc = classify_trade_outcome(status="SL", tp1_hit_at=ISO, tp2_hit_at=ISO, sl_hit_at=ISO_LATER)
    assert oc.outcome == "WIN"
    assert oc.winrate_bucket == BUCKET_WIN
    assert oc.max_tp_hit == 2


# ── 4. TP3 = FULL_WIN ─────────────────────────────────────────────────────────
def test_tp3_is_full_win():
    oc = classify_trade_outcome(status="TP3", tp3_hit_at=ISO)
    assert oc.outcome == "FULL_WIN"
    assert oc.winrate_bucket == BUCKET_WIN
    assert oc.max_tp_hit == 3


# ── 5. Manual close in profit = WIN ───────────────────────────────────────────
def test_manual_close_profit_is_win():
    oc = classify_trade_outcome(status="CLOSED", realized_pnl=3.4)
    assert oc.outcome == "WIN"
    assert oc.winrate_bucket == BUCKET_WIN


# ── 6. Manual close at a loss = LOSS ──────────────────────────────────────────
def test_manual_close_loss_is_loss():
    oc = classify_trade_outcome(status="CLOSED", realized_pnl=-2.1)
    assert oc.outcome == "LOSS"
    assert oc.winrate_bucket == BUCKET_LOSS


# ── 7. Break-even = BREAKEVEN ─────────────────────────────────────────────────
def test_break_even_is_breakeven():
    oc = classify_trade_outcome(status="BE")
    assert oc.outcome == "BREAKEVEN"
    assert oc.winrate_bucket == BUCKET_BREAKEVEN

    # Manual flat close also reads as break-even.
    oc2 = classify_trade_outcome(status="CLOSED", realized_pnl=0.0)
    assert oc2.outcome == "BREAKEVEN"


# ── 8. OPEN is excluded from winrate ──────────────────────────────────────────
def test_open_excluded_from_winrate():
    oc = classify_trade_outcome(status="OPEN")
    assert oc.outcome == "OPEN"
    assert oc.winrate_bucket == BUCKET_OPEN


# ── 9. Historical TP1/TP2/TP3 status still counts as a win ────────────────────
def test_legacy_tp_status_counts_as_win():
    for status in ("TP1", "TP2", "TP3"):
        sig = {"status": status, "pnl_pct": 5.0, "diagnostics": None}
        assert winrate_bucket_for_signal(sig) == BUCKET_WIN


# ── 10. Legacy SL status with tp_history.max_tp_hit >= 1 counts as a win ───────
def test_legacy_sl_status_with_tp_history_is_win():
    diagnostics = json.dumps(
        {
            "tp_history": {
                "tp1_hit_at": ISO,
                "sl_hit_at": ISO_LATER,
                "max_tp_hit": 1,
                "first_exit_event": "TP1",
                "final_exit_event": "SL",
            }
        }
    )
    sig = {"status": "SL", "pnl_pct": -1.5, "diagnostics": diagnostics}
    oc = outcome_for_signal(sig)
    assert oc.outcome == "PARTIAL_WIN"
    assert winrate_bucket_for_signal(sig) == BUCKET_WIN


# ── 11. A pure-SL legacy signal (no TP history) stays a loss ──────────────────
def test_legacy_sl_status_without_tp_history_is_loss():
    sig = {"status": "SL", "pnl_pct": -2.0, "diagnostics": None}
    assert winrate_bucket_for_signal(sig) == BUCKET_LOSS


# ── record_exit_event builds coherent tp_history ──────────────────────────────
def test_record_exit_event_accumulates_history():
    t1 = datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    diag = record_exit_event(None, "TP1", event_time=t1, realized_pnl=4.0)
    hist = diag["tp_history"]
    assert hist["tp1_hit_at"] is not None
    assert hist["max_tp_hit"] == 1
    assert hist["first_exit_event"] == "TP1"
    assert hist["trade_outcome"] == "PARTIAL_WIN"

    # Later SL must NOT erase the TP1 win.
    diag2 = record_exit_event(json.dumps(diag), "SL", event_time=t2, realized_pnl=-1.0)
    hist2 = diag2["tp_history"]
    assert hist2["tp1_hit_at"] == hist["tp1_hit_at"]  # preserved
    assert hist2["sl_hit_at"] is not None
    assert hist2["max_tp_hit"] == 1
    assert hist2["first_exit_event"] == "TP1"
    assert hist2["final_exit_event"] == "SL"
    assert hist2["trade_outcome"] == "PARTIAL_WIN"


def test_record_exit_event_direct_tp2_implies_tp1():
    diag = record_exit_event(None, "TP2", realized_pnl=8.0)
    hist = diag["tp_history"]
    assert hist["max_tp_hit"] == 2
    assert hist["tp1_hit_at"] is not None
    assert hist["tp2_hit_at"] is not None
    assert hist["trade_outcome"] == "WIN"
