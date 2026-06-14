"""
Trade duration / holding-time display in Telegram signal updates.

Pure formatting tests — no network, DB, or running bot. They pin the exact
``⏱ <duration> in trade`` style and the UTC-safe / missing-timestamp fallbacks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.telegram_bot.formatter import (
    format_event,
    format_trade_duration,
    trade_duration_context,
    trade_duration_line,
)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (14 * 60, "14m"),  # < 60 minutes
        (0, "0m"),
        (59 * 60, "59m"),
        ((2 * 60 + 14) * 60, "2h 14m"),  # < 24 hours
        (60 * 60, "1h 0m"),  # exactly one hour
        ((5 * 60 + 22) * 60, "5h 22m"),
        (27 * 3600, "1d 3h"),  # >= 24 hours -> days + hours, no minutes
        (24 * 3600, "1d 0h"),
        (50 * 3600 + 30 * 60, "2d 2h"),
    ],
)
def test_format_trade_duration_formats(seconds, expected):
    assert format_trade_duration(seconds) == expected


def test_format_trade_duration_rounds_to_nearest_minute():
    # 14m 29s rounds down, 14m 31s rounds up — never any seconds in output.
    assert format_trade_duration(14 * 60 + 29) == "14m"
    assert format_trade_duration(14 * 60 + 31) == "15m"


def test_format_trade_duration_missing_returns_none():
    assert format_trade_duration(None) is None
    assert format_trade_duration("not-a-number") is None


def test_format_trade_duration_negative_clamped():
    assert format_trade_duration(-100) == "0m"


def test_trade_duration_context_and_line():
    opened = datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)
    event = opened + timedelta(hours=2, minutes=14)
    ctx = trade_duration_context(opened, event)
    assert ctx["trade_duration"] == "2h 14m"
    assert ctx["trade_duration_seconds"] == pytest.approx((2 * 60 + 14) * 60)
    assert trade_duration_line(opened, event) == "⏱ 2h 14m in trade"


def test_trade_duration_timezone_safe_naive_treated_as_utc():
    # Naive datetimes are interpreted as UTC, not local time.
    opened_naive = datetime(2026, 6, 11, 10, 0)
    event_naive = datetime(2026, 6, 11, 12, 14)
    assert trade_duration_line(opened_naive, event_naive) == "⏱ 2h 14m in trade"

    # Mixed aware/naive must still agree on the same wall-clock UTC delta.
    opened_aware = datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)
    assert trade_duration_line(opened_aware, event_naive) == "⏱ 2h 14m in trade"


def test_trade_duration_iso_string_input():
    line = trade_duration_line("2026-06-11T10:00:00Z", "2026-06-12T13:00:00Z")
    assert line == "⏱ 1d 3h in trade"


def test_trade_duration_missing_opened_at_falls_back():
    assert trade_duration_context(None) == {}
    assert trade_duration_line(None) == ""


def test_format_event_includes_duration_line():
    opened = datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc)
    event = opened + timedelta(minutes=14)
    payload = {
        "event": "TP2",
        "symbol": "HOMEUSDT",
        "side": "LONG",
        "pnl_pct": 10.61,
        "opened_at": opened,
        "event_time": event,
    }
    msg = format_event(payload)
    assert "⚡ ARGUS QUANT" in msg
    assert "⏱ 14m in trade" in msg


def test_format_event_without_opened_at_keeps_branding_no_duration():
    payload = {
        "event": "SL",
        "symbol": "BTCUSDT",
        "side": "SHORT",
        "pnl_pct": -1.2,
    }
    msg = format_event(payload)
    assert "⚡ ARGUS QUANT" in msg
    assert "in trade" not in msg
