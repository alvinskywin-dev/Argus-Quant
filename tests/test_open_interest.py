"""
Tests for the Sprint 11A Open Interest Engine.
"""
from __future__ import annotations

import pytest

from app.market_data.open_interest import OISnapshot, compute_oi_score


# ── compute_oi_score ──────────────────────────────────────────────────────────

class TestComputeOIScore:
    def test_long_price_up_oi_up_returns_plus15(self):
        assert compute_oi_score("LONG", price_change_pct=1.5, oi_change_pct=2.0) == 15

    def test_long_price_up_oi_down_returns_minus10(self):
        assert compute_oi_score("LONG", price_change_pct=0.8, oi_change_pct=-1.2) == -10

    def test_short_price_down_oi_up_returns_plus15(self):
        assert compute_oi_score("SHORT", price_change_pct=-1.0, oi_change_pct=3.0) == 15

    def test_short_price_down_oi_down_returns_minus10(self):
        assert compute_oi_score("SHORT", price_change_pct=-0.5, oi_change_pct=-2.5) == -10

    def test_long_price_flat_returns_zero(self):
        assert compute_oi_score("LONG", price_change_pct=0.0, oi_change_pct=1.0) == 0

    def test_long_price_down_returns_zero(self):
        # Price moving against trade direction → no score
        assert compute_oi_score("LONG", price_change_pct=-1.0, oi_change_pct=2.0) == 0

    def test_short_price_up_returns_zero(self):
        assert compute_oi_score("SHORT", price_change_pct=1.0, oi_change_pct=-1.0) == 0

    def test_unknown_side_returns_zero(self):
        assert compute_oi_score("FLAT", price_change_pct=1.0, oi_change_pct=1.0) == 0

    def test_both_flat_returns_zero(self):
        assert compute_oi_score("LONG", price_change_pct=0.0, oi_change_pct=0.0) == 0

    def test_score_is_integer(self):
        result = compute_oi_score("LONG", price_change_pct=0.3, oi_change_pct=0.1)
        assert isinstance(result, int)


# ── OISnapshot dataclass ──────────────────────────────────────────────────────

class TestOISnapshot:
    def _make(self, **kwargs) -> OISnapshot:
        defaults = dict(
            symbol="BTCUSDT",
            open_interest=1_200_000.0,
            oi_change_5m=0.5,
            oi_change_15m=1.2,
            oi_change_1h=-0.3,
            price_change_pct=0.8,
            oi_score=15,
        )
        return OISnapshot(**{**defaults, **kwargs})

    def test_snapshot_fields_stored(self):
        snap = self._make()
        assert snap.symbol == "BTCUSDT"
        assert snap.open_interest == 1_200_000.0
        assert snap.oi_change_5m == 0.5
        assert snap.oi_change_15m == 1.2
        assert snap.oi_change_1h == -0.3
        assert snap.price_change_pct == 0.8
        assert snap.oi_score == 15

    def test_bullish_snapshot(self):
        snap = self._make(price_change_pct=1.5, oi_change_15m=2.0, oi_score=15)
        assert snap.oi_score == 15

    def test_bearish_snapshot(self):
        snap = self._make(price_change_pct=0.5, oi_change_15m=-1.0, oi_score=-10)
        assert snap.oi_score == -10

    def test_neutral_snapshot(self):
        snap = self._make(price_change_pct=0.0, oi_change_15m=0.0, oi_score=0)
        assert snap.oi_score == 0


# ── confidence adjustment bounds ─────────────────────────────────────────────

class TestConfidenceAdjustment:
    """Verify that OI score adjustment clamps correctly at 0 and 100."""

    def _adjust(self, base: float, oi_score: int) -> float:
        return round(max(0.0, min(100.0, base + oi_score)), 1)

    def test_plus15_boosts_within_cap(self):
        assert self._adjust(80.0, 15) == 95.0

    def test_plus15_caps_at_100(self):
        assert self._adjust(90.0, 15) == 100.0

    def test_minus10_reduces(self):
        assert self._adjust(80.0, -10) == 70.0

    def test_minus10_floors_at_zero(self):
        assert self._adjust(5.0, -10) == 0.0

    def test_zero_score_no_change(self):
        assert self._adjust(78.5, 0) == 78.5
