"""
Protected-stop broadcast suppression.

A stop hit after a take-profit is a protected win, already announced when the TP
hit. should_broadcast_event suppresses the redundant "WIN LOCKED" / "PARTIAL WIN"
notification (unless re-enabled), while genuine stops and TP events still send.
"""

from __future__ import annotations

from app.telegram_bot.bot import should_broadcast_event


def test_protected_sl_suppressed_by_default():
    # SL after TP1 / TP2+ → not broadcast.
    assert should_broadcast_event("SL", max_tp_hit=1, notify_protected_sl=False) is False
    assert should_broadcast_event("SL", max_tp_hit=2, notify_protected_sl=False) is False
    assert should_broadcast_event("SL", max_tp_hit=3, notify_protected_sl=False) is False


def test_genuine_stop_still_broadcast():
    # SL with no TP ever hit → a real loss, still broadcast.
    assert should_broadcast_event("SL", max_tp_hit=0, notify_protected_sl=False) is True


def test_tp_events_always_broadcast():
    for ev in ("TP1", "TP2", "TP3"):
        assert should_broadcast_event(ev, max_tp_hit=0, notify_protected_sl=False) is True
        assert should_broadcast_event(ev, max_tp_hit=2, notify_protected_sl=False) is True


def test_protected_sl_can_be_reenabled():
    assert should_broadcast_event("SL", max_tp_hit=1, notify_protected_sl=True) is True
    assert should_broadcast_event("SL", max_tp_hit=2, notify_protected_sl=True) is True
