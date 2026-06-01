"""
Sprint 21C — position recovery scenarios.

The recovery engine (recover_user_positions) is DB+adapter bound and is
exercised by tests/e2e_*. Here we cover the recovery DECISION contract with the
pure building blocks the engine uses, so the safety semantics are pinned:

  * exchange position with no DB row  -> import as RECOVERED (orphan)
  * DB position gone from exchange    -> CLOSED_UNKNOWN
  * TP/SL missing after a restart     -> UNSAFE / retry
"""
from __future__ import annotations

from app.recovery import tp_sl
from app.reconciliation.engine import (
    DB_POSITION_MISSING_ON_EXCHANGE,
    EXCHANGE_POSITION_MISSING_IN_DB,
    SEV_CRITICAL,
    reconcile_symbol,
)


def _pos(side="LONG", qty=1.0, entry=100.0, lev=5, margin="isolated", status="OPEN"):
    return {"side": side, "qty": qty, "entry_price": entry,
            "leverage": lev, "margin_type": margin, "status": status}


def test_orphan_exchange_position_is_recoverable_critical():
    # Exchange has a position the DB never recorded -> recovery imports RECOVERED.
    issues = reconcile_symbol("ETHUSDT", db_pos=None, ex_pos=_pos())
    assert len(issues) == 1
    assert issues[0].issue_type == EXCHANGE_POSITION_MISSING_IN_DB
    assert issues[0].severity == SEV_CRITICAL
    # The recovered import keeps the exchange's true size/side.
    assert issues[0].exchange_state["qty"] == 1.0


def test_db_position_vanished_becomes_closed_unknown():
    issues = reconcile_symbol("ETHUSDT", db_pos=_pos(), ex_pos=None)
    assert len(issues) == 1
    assert issues[0].issue_type == DB_POSITION_MISSING_ON_EXCHANGE
    assert issues[0].severity == SEV_CRITICAL


def test_restart_with_open_position_but_no_protective_orders_is_unsafe():
    # Simulate: position survived restart, exchange shows NO TP/SL working.
    status = tp_sl.compute_tp_sl_status(has_tp=False, has_sl=False)
    assert status == tp_sl.MISSING_BOTH
    assert tp_sl.is_unsafe_status(status)


def test_restart_with_stop_loss_intact_is_safe():
    status = tp_sl.compute_tp_sl_status(has_tp=True, has_sl=True)
    assert status == tp_sl.SYNCED
    assert tp_sl.is_protected(status)


def test_matched_position_no_drift_means_no_recovery_action():
    assert reconcile_symbol("ETHUSDT", db_pos=_pos(), ex_pos=_pos()) == []
