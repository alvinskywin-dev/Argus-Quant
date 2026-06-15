"""
Timezone System V1 — backend timezone utilities.

Single source of truth for timezone handling across the platform. The database
stores **UTC only** and APIs serialize **UTC ISO**; conversion to a user's
preferred timezone happens for *display* only (frontend, or these helpers when a
server-rendered string is unavoidable).

Design rules (enforced here):
  * UTC is the only stored/serialized zone — these helpers never persist a
    converted value.
  * Naive datetimes are treated as UTC (the codebase stores tz-aware UTC, but
    legacy/SQLite rows can come back naive).
  * Nothing here raises on ``None`` or on a bad timezone — bad input degrades to
    UTC / ``None`` so a display path can never crash a request.
  * Only the supported IANA zones are ever honored; anything else falls back to
    the default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

# ── supported zones ─────────────────────────────────────────────────
SUPPORTED_TIMEZONES = [
    "UTC",
    "Europe/London",
    "Asia/Phnom_Penh",
    "Asia/Ho_Chi_Minh",
    "America/New_York",
    "America/Los_Angeles",
]

DEFAULT_TIMEZONE = "UTC"

DateLike = Union[datetime, str, None]


# ── core ────────────────────────────────────────────────────────────


def utc_now() -> datetime:
    """Timezone-aware 'now' in UTC."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Return a tz-aware UTC datetime. A naive datetime is *assumed* to already be
    UTC (the storage contract); an aware datetime is converted to UTC. ``None``
    passes through unchanged.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_datetime(value: DateLike) -> Optional[datetime]:
    """Accept a datetime or an ISO string and return a tz-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Tolerate a trailing 'Z' (Python <3.11 fromisoformat can't parse it).
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return ensure_utc(datetime.fromisoformat(s))
        except ValueError:
            return None
    return None


def normalize_utc_iso(value: DateLike) -> Optional[str]:
    """
    Serialize any datetime/ISO-string to a canonical **UTC ISO** string with an
    explicit offset (``2026-06-01T17:06:23+00:00``). Returns ``None`` on ``None``
    or unparseable input. Never converts to a user zone.
    """
    dt = _coerce_datetime(value)
    return dt.isoformat() if dt is not None else None


# ── timezone selection ──────────────────────────────────────────────


def is_supported_timezone(tz: Optional[str]) -> bool:
    """True only for an exactly-supported IANA zone string."""
    return isinstance(tz, str) and tz in SUPPORTED_TIMEZONES


def safe_timezone(tz: Optional[str]) -> str:
    """Return ``tz`` if supported, else the default — never raises."""
    # Inline the check (rather than is_supported_timezone) so the type checker
    # narrows tz to str on the supported branch.
    return tz if isinstance(tz, str) and tz in SUPPORTED_TIMEZONES else DEFAULT_TIMEZONE


def _zoneinfo(tz: Optional[str]) -> ZoneInfo:
    name = safe_timezone(tz)
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — missing tzdata etc. -> UTC
        return ZoneInfo("UTC")


# ── display conversion (never persisted) ───────────────────────────


def to_user_timezone(value: DateLike, tz: Optional[str]) -> Optional[datetime]:
    """Convert a UTC value into the (supported) user zone for display only."""
    dt = _coerce_datetime(value)
    if dt is None:
        return None
    return dt.astimezone(_zoneinfo(tz))


def format_datetime_for_timezone(value: DateLike, tz: Optional[str]) -> Optional[str]:
    """
    Full display string, 24-hour, e.g. ``01 Jun 2026 18:06:23 UTC`` /
    ``01 Jun 2026 18:06:23 Asia/Phnom_Penh``. The suffix is the IANA zone name,
    matching the frontend formatter. Returns ``None`` on ``None``/bad input.
    """
    dt = to_user_timezone(value, tz)
    if dt is None:
        return None
    return f"{dt.strftime('%d %b %Y %H:%M:%S')} {safe_timezone(tz)}"


def format_short_datetime_for_timezone(value: DateLike, tz: Optional[str]) -> Optional[str]:
    """Short display string, e.g. ``01 Jun 18:06 UTC``. None-safe."""
    dt = to_user_timezone(value, tz)
    if dt is None:
        return None
    return f"{dt.strftime('%d %b %H:%M')} {safe_timezone(tz)}"
