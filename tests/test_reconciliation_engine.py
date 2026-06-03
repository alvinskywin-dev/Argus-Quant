"""
Sprint 21B — unit tests for the pure reconciliation drift detector.

No DB / no network: the DB-backed orchestration is exercised manually
(tests/e2e_*), but the safety-critical decision logic lives in
``reconcile_symbol`` and is fully covered here.
"""
from __future__ import annotations

from app.reconciliation.engine import (
    DB_POSITION_MISSING_ON_EXCHANGE,
    ENTRY_MISMATCH,
    EXCHANGE_POSITION_MISSING_IN_DB,
    LEVERAGE_MISMATCH,
    MODE_MISMATCH,
    SEV_CRITICAL,
    SIDE_MISMATCH,
    SIZE_MISMATCH,
    TP_SL_MISSING_IN_DB,
    TP_SL_MISSING_ON_EXCHANGE,
    reconcile_symbol,
)


def _pos(side="LONG", qty=1.0, entry=100.0, lev=5, margin="isolated", status="OPEN"):
    return {"side": side, "qty": qty, "entry_price": entry,
            "leverage": lev, "margin_type": margin, "status": status}


def _types(issues):
    return {i.issue_type for i in issues}


def test_db_position_missing_on_exchange():
    issues = reconcile_symbol("BTCUSDT", _pos(), None)
    assert _types(issues) == {DB_POSITION_MISSING_ON_EXCHANGE}
    assert issues[0].severity == SEV_CRITICAL


def test_exchange_position_missing_in_db_is_orphan():
    issues = reconcile_symbol("BTCUSDT", None, _pos())
    assert _types(issues) == {EXCHANGE_POSITION_MISSING_IN_DB}
    assert issues[0].severity == SEV_CRITICAL


def test_no_positions_no_issues():
    assert reconcile_symbol("BTCUSDT", None, None) == []


def test_matching_positions_no_issues():
    issues = reconcile_symbol("BTCUSDT", _pos(), _pos())
    assert issues == []


def test_side_mismatch_critical():
    issues = reconcile_symbol("BTCUSDT", _pos(side="LONG"), _pos(side="SHORT"))
    assert SIDE_MISMATCH in _types(issues)
    sev = [i.severity for i in issues if i.issue_type == SIDE_MISMATCH][0]
    assert sev == SEV_CRITICAL


def test_size_mismatch_small_is_warning_large_is_critical():
    small = reconcile_symbol("BTCUSDT", _pos(qty=1.0), _pos(qty=1.05))  # 5%
    big = reconcile_symbol("BTCUSDT", _pos(qty=1.0), _pos(qty=2.0))     # 100%
    small_sev = [i.severity for i in small if i.issue_type == SIZE_MISMATCH][0]
    big_sev = [i.severity for i in big if i.issue_type == SIZE_MISMATCH][0]
    assert small_sev == "WARNING"
    assert big_sev == "CRITICAL"


def test_tiny_size_diff_within_tolerance_ignored():
    issues = reconcile_symbol("BTCUSDT", _pos(qty=1.000), _pos(qty=1.005))  # 0.5% < 2%
    assert SIZE_MISMATCH not in _types(issues)


def test_entry_leverage_margin_mismatches():
    issues = reconcile_symbol(
        "BTCUSDT", _pos(entry=100, lev=5, margin="isolated"),
        _pos(entry=110, lev=10, margin="cross"))
    t = _types(issues)
    assert ENTRY_MISMATCH in t and LEVERAGE_MISMATCH in t and MODE_MISMATCH in t


def test_tp_sl_missing_on_exchange_is_critical():
    issues = reconcile_symbol(
        "BTCUSDT", _pos(), _pos(),
        db_protection={"has_tp": True, "has_sl": True},
        ex_protection={"has_tp": False, "has_sl": False})
    assert TP_SL_MISSING_ON_EXCHANGE in _types(issues)
    sev = [i.severity for i in issues if i.issue_type == TP_SL_MISSING_ON_EXCHANGE][0]
    assert sev == SEV_CRITICAL


def test_tp_sl_missing_in_db_is_info():
    issues = reconcile_symbol(
        "BTCUSDT", _pos(), _pos(),
        db_protection={"has_tp": False, "has_sl": False},
        ex_protection={"has_tp": True, "has_sl": True})
    assert TP_SL_MISSING_IN_DB in _types(issues)


def test_tp_sl_skipped_when_exchange_protection_unknown():
    # ex_protection=None means "open orders not fetched" -> no TP/SL assertions.
    issues = reconcile_symbol(
        "BTCUSDT", _pos(), _pos(),
        db_protection={"has_tp": True, "has_sl": True}, ex_protection=None)
    assert TP_SL_MISSING_ON_EXCHANGE not in _types(issues)
