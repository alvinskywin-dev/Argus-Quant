"""
Sprint 22E — News / Event Risk Filter.

Blocks NEW entries around abnormal macro / token-specific volatility windows:
CPI, FOMC, Fed rate, NFP, BTC-ETF headlines, major token unlocks, and Binance
listing / delisting events. It never closes or modifies existing positions and
never blocks monitoring, analytics, or paper mode — it only gates new live/auto
entries.

No fake data: the engine holds an in-memory event calendar that the caller
populates from a real source (an econ-calendar feed, an admin entry, etc.).
With an empty calendar it simply allows everything. With
`NEWS_EVENT_FILTER_ENABLED=false` it always allows, but still reports the
upcoming events for diagnostics / the public API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.config import _norm_base_symbol, settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MarketEvent:
    name: str  # e.g. "CPI", "FOMC", "BTC unlock"
    event_time: datetime  # tz-aware UTC
    severity: str = "HIGH"  # HIGH | MEDIUM | LOW
    symbols: List[str] = field(default_factory=list)  # empty == affects all (macro)
    kind: str = "MACRO"  # MACRO | UNLOCK | LISTING | DELISTING | NEWS

    def __post_init__(self):
        if self.event_time.tzinfo is None:
            self.event_time = self.event_time.replace(tzinfo=timezone.utc)
        self.name = self.name.strip()
        self.severity = (self.severity or "HIGH").upper()
        self.symbols = [s.strip().upper() for s in self.symbols if s.strip()]

    @property
    def is_high_impact(self) -> bool:
        if self.severity == "HIGH":
            return True
        token = self.name.split()[0].upper() if self.name else ""
        return token in settings.high_impact_event_set

    def affects(self, symbol: str) -> bool:
        if not self.symbols:
            return True  # macro: affects everything
        base = _norm_base_symbol(symbol)
        return any(_norm_base_symbol(s) == base for s in self.symbols)


@dataclass
class NewsRiskDecision:
    allowed: bool
    reason: str
    blocking_event: Optional[str] = None
    minutes_to_event: Optional[float] = None
    severity: Optional[str] = None
    enabled: bool = True

    def to_diagnostics(self) -> dict:
        return {
            "news_filter_enabled": self.enabled,
            "news_allowed": self.allowed,
            "news_block_reason": None if self.allowed else self.reason,
            "blocking_event": self.blocking_event,
            "minutes_to_event": (
                round(self.minutes_to_event, 1) if self.minutes_to_event is not None else None
            ),
            "event_severity": self.severity,
        }


class NewsEventCalendar:
    """In-memory event registry. Populate from a real feed / admin input."""

    def __init__(self) -> None:
        self._events: List[MarketEvent] = []

    def add(self, event: MarketEvent) -> None:
        self._events.append(event)

    def clear(self) -> None:
        self._events = []

    def bulk_load(self, events: List[MarketEvent]) -> None:
        self._events = list(events)

    def upcoming(
        self, within_hours: float = 48, now: Optional[datetime] = None
    ) -> List[MarketEvent]:
        now = now or _utcnow()
        horizon = now + timedelta(hours=within_hours)
        out = [e for e in self._events if now - timedelta(hours=6) <= e.event_time <= horizon]
        return sorted(out, key=lambda e: e.event_time)

    def all(self) -> List[MarketEvent]:
        return list(self._events)


# Process-wide calendar (caller fills it; tests use their own instance).
_calendar = NewsEventCalendar()


def get_calendar() -> NewsEventCalendar:
    return _calendar


def _window_minutes(event: MarketEvent) -> tuple[int, int]:
    """Pre/post block windows. High-impact events use the configured windows;
    lower-impact ones use half."""
    pre = settings.pre_event_block_minutes
    post = settings.post_event_block_minutes
    if not event.is_high_impact:
        pre = pre // 2
        post = post // 2
    return pre, post


def can_open_entry(
    symbol: str,
    *,
    now: Optional[datetime] = None,
    calendar: Optional[NewsEventCalendar] = None,
) -> NewsRiskDecision:
    """Decide whether a new entry on ``symbol`` is allowed given the calendar."""
    cal = calendar or _calendar
    now = now or _utcnow()

    if not settings.news_event_filter_enabled:
        return NewsRiskDecision(allowed=True, reason="news event filter disabled", enabled=False)

    for event in sorted(cal.all(), key=lambda e: e.event_time):
        if not event.affects(symbol):
            continue
        pre, post = _window_minutes(event)
        block_start = event.event_time - timedelta(minutes=pre)
        block_end = event.event_time + timedelta(minutes=post)
        if block_start <= now <= block_end:
            delta_min = (event.event_time - now).total_seconds() / 60.0
            phase = "pre-event" if now <= event.event_time else "post-event"
            return NewsRiskDecision(
                allowed=False,
                reason=f"{phase} block around {event.name} ({event.severity})",
                blocking_event=event.name,
                minutes_to_event=delta_min,
                severity=event.severity,
                enabled=True,
            )

    return NewsRiskDecision(allowed=True, reason="no active event window", enabled=True)


def news_risk_snapshot(
    *, now: Optional[datetime] = None, calendar: Optional[NewsEventCalendar] = None
) -> dict:
    """Payload for GET /api/public/news-risk."""
    cal = calendar or _calendar
    now = now or _utcnow()
    upcoming = cal.upcoming(now=now)
    return {
        "enabled": settings.news_event_filter_enabled,
        "now": now.isoformat(),
        "pre_event_block_minutes": settings.pre_event_block_minutes,
        "post_event_block_minutes": settings.post_event_block_minutes,
        "high_impact_events": sorted(settings.high_impact_event_set),
        "upcoming_events": [
            {
                "name": e.name,
                "event_time": e.event_time.isoformat(),
                "severity": e.severity,
                "kind": e.kind,
                "symbols": e.symbols or ["*"],
                "minutes_to_event": round((e.event_time - now).total_seconds() / 60.0, 1),
                "high_impact": e.is_high_impact,
            }
            for e in upcoming
        ],
        "active_block": not can_open_entry("BTCUSDT", now=now, calendar=cal).allowed,
    }
