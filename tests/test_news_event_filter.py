"""Sprint 22E — News / Event Risk Filter (pure, calendar-driven)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.risk.news_event_filter import (
    MarketEvent,
    NewsEventCalendar,
    can_open_entry,
    news_risk_snapshot,
)

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    keys = [
        "news_event_filter_enabled",
        "pre_event_block_minutes",
        "post_event_block_minutes",
        "high_impact_events",
    ]
    saved = {k: getattr(settings, k) for k in keys}
    settings.news_event_filter_enabled = True
    settings.pre_event_block_minutes = 60
    settings.post_event_block_minutes = 30
    settings.high_impact_events = "CPI,FOMC,NFP,FED"
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _cal(*events):
    cal = NewsEventCalendar()
    cal.bulk_load(list(events))
    return cal


# ── disabled ─────────────────────────────────────────────────────────────────
def test_disabled_allows(cfg):
    cfg.news_event_filter_enabled = False
    cal = _cal(MarketEvent("CPI", NOW))
    d = can_open_entry("BTCUSDT", now=NOW, calendar=cal)
    assert d.allowed is True
    assert d.enabled is False


# ── pre / post windows ───────────────────────────────────────────────────────
def test_pre_event_block(cfg):
    event = MarketEvent("CPI", NOW + timedelta(minutes=30))  # 30m away, within 60m pre
    d = can_open_entry("BTCUSDT", now=NOW, calendar=_cal(event))
    assert d.allowed is False
    assert "pre-event" in d.reason
    assert d.blocking_event == "CPI"


def test_post_event_block(cfg):
    event = MarketEvent("FOMC", NOW - timedelta(minutes=15))  # 15m ago, within 30m post
    d = can_open_entry("BTCUSDT", now=NOW, calendar=_cal(event))
    assert d.allowed is False
    assert "post-event" in d.reason


def test_outside_window_allows(cfg):
    event = MarketEvent("CPI", NOW + timedelta(hours=3))
    d = can_open_entry("BTCUSDT", now=NOW, calendar=_cal(event))
    assert d.allowed is True


def test_exactly_at_event_blocked(cfg):
    event = MarketEvent("NFP", NOW)
    d = can_open_entry("BTCUSDT", now=NOW, calendar=_cal(event))
    assert d.allowed is False


# ── severity windows ─────────────────────────────────────────────────────────
def test_low_impact_uses_half_window(cfg):
    # MEDIUM severity, non-high-impact name -> half of 60m = 30m pre window.
    event = MarketEvent("RandomPR", NOW + timedelta(minutes=45), severity="MEDIUM")
    d = can_open_entry("BTCUSDT", now=NOW, calendar=_cal(event))
    assert d.allowed is True  # 45m > 30m half-window


def test_high_severity_full_window(cfg):
    event = MarketEvent("RandomPR", NOW + timedelta(minutes=45), severity="HIGH")
    d = can_open_entry("BTCUSDT", now=NOW, calendar=_cal(event))
    assert d.allowed is False  # 45m < 60m full window


def test_high_impact_name_is_high_impact(cfg):
    event = MarketEvent("CPI release", NOW, severity="MEDIUM")
    assert event.is_high_impact is True


# ── symbol scoping ───────────────────────────────────────────────────────────
def test_macro_event_affects_all_symbols(cfg):
    event = MarketEvent("CPI", NOW + timedelta(minutes=20))  # no symbols = macro
    assert can_open_entry("ETHUSDT", now=NOW, calendar=_cal(event)).allowed is False
    assert can_open_entry("DOGEUSDT", now=NOW, calendar=_cal(event)).allowed is False


def test_token_event_scoped(cfg):
    event = MarketEvent(
        "ARB unlock",
        NOW + timedelta(minutes=20),
        severity="HIGH",
        symbols=["ARBUSDT"],
        kind="UNLOCK",
    )
    assert can_open_entry("ARBUSDT", now=NOW, calendar=_cal(event)).allowed is False
    assert can_open_entry("BTCUSDT", now=NOW, calendar=_cal(event)).allowed is True


def test_token_event_symbol_normalised(cfg):
    event = MarketEvent("ARB unlock", NOW, severity="HIGH", symbols=["ARB"])
    assert can_open_entry("ARBUSDT", now=NOW, calendar=_cal(event)).allowed is False


# ── empty calendar ───────────────────────────────────────────────────────────
def test_empty_calendar_allows(cfg):
    assert can_open_entry("BTCUSDT", now=NOW, calendar=NewsEventCalendar()).allowed is True


def test_naive_datetime_coerced_utc():
    naive = datetime(2026, 6, 3, 12, 0)
    event = MarketEvent("CPI", naive)
    assert event.event_time.tzinfo is not None


# ── snapshot / API payload ───────────────────────────────────────────────────
def test_snapshot_lists_upcoming(cfg):
    cal = _cal(
        MarketEvent("CPI", NOW + timedelta(hours=2)),
        MarketEvent("FOMC", NOW + timedelta(hours=10)),
    )
    snap = news_risk_snapshot(now=NOW, calendar=cal)
    assert snap["enabled"] is True
    assert len(snap["upcoming_events"]) == 2
    assert snap["upcoming_events"][0]["name"] == "CPI"  # sorted by time
    assert "minutes_to_event" in snap["upcoming_events"][0]


def test_snapshot_active_block_flag(cfg):
    cal = _cal(MarketEvent("CPI", NOW + timedelta(minutes=10)))
    snap = news_risk_snapshot(now=NOW, calendar=cal)
    assert snap["active_block"] is True


def test_calendar_add_and_clear():
    cal = NewsEventCalendar()
    cal.add(MarketEvent("CPI", NOW))
    assert len(cal.all()) == 1
    cal.clear()
    assert cal.all() == []


def test_diagnostics_shape(cfg):
    d = can_open_entry("BTCUSDT", now=NOW, calendar=_cal(MarketEvent("CPI", NOW))).to_diagnostics()
    assert d["news_filter_enabled"] is True
    assert d["news_allowed"] is False
    assert d["blocking_event"] == "CPI"
