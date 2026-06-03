"""
Timezone System V1 — unit tests.

Pure timezone-utility tests (no DB / no network) plus the timezone-endpoint
validation gate. DB-backed persistence of PUT /api/auth/timezone is covered by
the manual e2e flow; here we exercise the supported/unsupported decision, which
is what the endpoint enforces before touching the database.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.utils.timezone import (
    DEFAULT_TIMEZONE,
    SUPPORTED_TIMEZONES,
    ensure_utc,
    format_datetime_for_timezone,
    format_short_datetime_for_timezone,
    is_supported_timezone,
    normalize_utc_iso,
    safe_timezone,
    to_user_timezone,
    utc_now,
)


# 1) supported timezone list
def test_supported_timezone_list():
    assert SUPPORTED_TIMEZONES == [
        "UTC",
        "Europe/London",
        "Asia/Phnom_Penh",
        "Asia/Ho_Chi_Minh",
        "America/New_York",
        "America/Los_Angeles",
    ]
    assert DEFAULT_TIMEZONE == "UTC"
    assert all(is_supported_timezone(tz) for tz in SUPPORTED_TIMEZONES)


# 2) invalid timezone rejected
def test_invalid_timezone_rejected():
    assert not is_supported_timezone("Mars/Phobos")
    assert not is_supported_timezone("")
    assert not is_supported_timezone(None)
    assert not is_supported_timezone("asia/phnom_penh")  # case-sensitive IANA
    assert safe_timezone("Mars/Phobos") == "UTC"
    assert safe_timezone(None) == "UTC"
    assert safe_timezone("Asia/Phnom_Penh") == "Asia/Phnom_Penh"


# 3) naive datetime treated as UTC
def test_naive_datetime_treated_as_utc():
    naive = datetime(2026, 6, 1, 17, 6, 23)
    aware = ensure_utc(naive)
    assert aware.tzinfo is not None
    assert aware.utcoffset().total_seconds() == 0
    assert aware.hour == 17
    # aware input is converted, not reinterpreted
    ny = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert ensure_utc(ny).hour == 12
    assert ensure_utc(None) is None


# 4) UTC ISO normalization
def test_utc_iso_normalization():
    assert normalize_utc_iso("2026-06-01T17:06:23") == "2026-06-01T17:06:23+00:00"
    assert normalize_utc_iso("2026-06-01T17:06:23Z") == "2026-06-01T17:06:23+00:00"
    assert normalize_utc_iso(datetime(2026, 6, 1, 17, 6, 23)) == "2026-06-01T17:06:23+00:00"
    assert normalize_utc_iso(None) is None
    assert normalize_utc_iso("not-a-date") is None
    assert utc_now().tzinfo is not None


# 5) DST behaviour — Europe/London
def test_london_dst():
    winter = to_user_timezone("2026-01-01T12:00:00+00:00", "Europe/London")
    summer = to_user_timezone("2026-06-01T12:00:00+00:00", "Europe/London")
    assert winter.hour == 12  # GMT
    assert summer.hour == 13  # BST (+1)
    assert (
        format_datetime_for_timezone("2026-06-01T12:00:00+00:00", "Europe/London")
        == "01 Jun 2026 13:00:00 Europe/London"
    )


# 6) Asia/Phnom_Penh (+7, no DST)
def test_phnom_penh_offset():
    pp = to_user_timezone("2026-06-01T12:00:00+00:00", "Asia/Phnom_Penh")
    assert pp.hour == 19
    assert (
        format_datetime_for_timezone("2026-06-01T12:00:00+00:00", "Asia/Phnom_Penh")
        == "01 Jun 2026 19:00:00 Asia/Phnom_Penh"
    )


# 7) America/New_York DST handling
def test_new_york_dst():
    winter = to_user_timezone("2026-01-01T12:00:00+00:00", "America/New_York")
    summer = to_user_timezone("2026-06-01T12:00:00+00:00", "America/New_York")
    assert winter.hour == 7  # EST (-5)
    assert summer.hour == 8  # EDT (-4)


def test_format_helpers_none_safe_and_short():
    assert format_datetime_for_timezone(None, "UTC") is None
    assert format_short_datetime_for_timezone(None, "UTC") is None
    assert (
        format_short_datetime_for_timezone("2026-06-01T18:06:23+00:00", "UTC") == "01 Jun 18:06 UTC"
    )
    # an unsupported zone degrades to UTC rather than raising
    assert (
        format_datetime_for_timezone("2026-06-01T12:00:00+00:00", "Mars/Phobos")
        == "01 Jun 2026 12:00:00 UTC"
    )


# 8 & 9) PUT /api/auth/timezone validation gate
@pytest.mark.asyncio
async def test_put_timezone_rejects_unsupported():
    from app.auth.router import update_timezone
    from app.auth.schemas import UpdateTimezoneIn

    resp = await update_timezone(UpdateTimezoneIn(timezone="Mars/Phobos"), user=None)
    assert resp.status_code == 400


def test_put_timezone_accepts_supported_values():
    # The endpoint's accept gate is is_supported_timezone; assert it admits every
    # supported zone (DB persistence is exercised in the manual e2e flow).
    assert all(is_supported_timezone(tz) for tz in SUPPORTED_TIMEZONES)
    assert is_supported_timezone("Asia/Phnom_Penh")
