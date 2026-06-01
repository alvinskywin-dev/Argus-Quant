"""
Sprint 21C — unit tests for the pure TP/SL status logic.

The DB-backed recovery orchestration (sync_tp_sl_for_position,
recover_user_positions) is exercised manually; the safety-critical decision —
"is this position protected?" — is pure and fully covered here.
"""
from __future__ import annotations

from app.recovery import tp_sl


def test_both_present_is_synced():
    assert tp_sl.compute_tp_sl_status(True, True) == tp_sl.SYNCED


def test_missing_sl():
    assert tp_sl.compute_tp_sl_status(has_tp=True, has_sl=False) == tp_sl.MISSING_SL


def test_missing_tp():
    assert tp_sl.compute_tp_sl_status(has_tp=False, has_sl=True) == tp_sl.MISSING_TP


def test_missing_both():
    assert tp_sl.compute_tp_sl_status(False, False) == tp_sl.MISSING_BOTH


def test_sl_only_position_is_synced_when_sl_present():
    # Position opened with stop-loss only (no TP target) -> TP not expected.
    assert tp_sl.compute_tp_sl_status(
        has_tp=False, has_sl=True, expected_tp=False) == tp_sl.SYNCED


def test_tp_only_position_missing_tp():
    assert tp_sl.compute_tp_sl_status(
        has_tp=False, has_sl=False, expected_sl=False) == tp_sl.MISSING_TP


def test_unprotected_target_position_is_unsafe():
    # A position that expects a stop-loss but has none is unsafe.
    status = tp_sl.compute_tp_sl_status(has_tp=True, has_sl=False)
    assert tp_sl.is_unsafe_status(status)
    assert not tp_sl.is_protected(status)


def test_missing_tp_only_is_not_unsafe():
    # Missing a take-profit (but stop-loss present) is not "unsafe" — capital is
    # still protected on the downside.
    status = tp_sl.compute_tp_sl_status(has_tp=False, has_sl=True)
    assert not tp_sl.is_unsafe_status(status)


def test_explicit_unsafe_flag():
    assert tp_sl.is_unsafe_status(tp_sl.UNSAFE)


def test_synced_is_protected():
    assert tp_sl.is_protected(tp_sl.SYNCED)
    assert not tp_sl.is_protected(tp_sl.MISSING_BOTH)
