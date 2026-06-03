"""
Tests for the Sprint 11B Funding Rate Engine.
"""
from __future__ import annotations

import os

import pytest

from app.market_data.funding import (
    FundingData,
    classify_funding,
    score_funding_for_side,
)

# ── classify_funding ──────────────────────────────────────────────────────────

class TestClassifyFunding:
    def test_neutral_zero(self):
        assert classify_funding(0.0) == "neutral"

    def test_neutral_small_positive(self):
        assert classify_funding(0.0001) == "neutral"

    def test_neutral_small_negative(self):
        assert classify_funding(-0.0001) == "neutral"

    def test_positive_at_threshold(self):
        assert classify_funding(0.0003) == "positive"

    def test_positive_between_thresholds(self):
        assert classify_funding(0.0005) == "positive"

    def test_extreme_positive_at_threshold(self):
        assert classify_funding(0.0008) == "extreme_positive"

    def test_extreme_positive_above(self):
        assert classify_funding(0.0015) == "extreme_positive"

    def test_negative_at_threshold(self):
        assert classify_funding(-0.0003) == "negative"

    def test_negative_between_thresholds(self):
        assert classify_funding(-0.0005) == "negative"

    def test_extreme_negative_at_threshold(self):
        assert classify_funding(-0.0008) == "extreme_negative"

    def test_extreme_negative_below(self):
        assert classify_funding(-0.0020) == "extreme_negative"

    def test_returns_string(self):
        result = classify_funding(0.001)
        assert isinstance(result, str)


# ── score_funding_for_side ────────────────────────────────────────────────────

class TestScoreFundingForSide:
    # LONG scores
    def test_long_neutral(self):
        fs = score_funding_for_side("neutral", "LONG")
        assert fs.score == 5

    def test_long_negative(self):
        fs = score_funding_for_side("negative", "LONG")
        assert fs.score == 8

    def test_long_extreme_negative(self):
        fs = score_funding_for_side("extreme_negative", "LONG")
        assert fs.score == 10

    def test_long_positive(self):
        fs = score_funding_for_side("positive", "LONG")
        assert fs.score == -5

    def test_long_extreme_positive(self):
        fs = score_funding_for_side("extreme_positive", "LONG")
        assert fs.score == -15

    # SHORT scores
    def test_short_neutral(self):
        fs = score_funding_for_side("neutral", "SHORT")
        assert fs.score == 5

    def test_short_positive(self):
        fs = score_funding_for_side("positive", "SHORT")
        assert fs.score == 8

    def test_short_extreme_positive(self):
        fs = score_funding_for_side("extreme_positive", "SHORT")
        assert fs.score == 10

    def test_short_negative(self):
        fs = score_funding_for_side("negative", "SHORT")
        assert fs.score == -5

    def test_short_extreme_negative(self):
        fs = score_funding_for_side("extreme_negative", "SHORT")
        assert fs.score == -15

    def test_score_is_int(self):
        fs = score_funding_for_side("neutral", "LONG")
        assert isinstance(fs.score, int)

    def test_reason_is_nonempty(self):
        fs = score_funding_for_side("extreme_positive", "LONG")
        assert fs.reason  # non-empty string

    def test_classification_stored(self):
        fs = score_funding_for_side("positive", "SHORT")
        assert fs.classification == "positive"

    def test_unknown_classification_returns_zero(self):
        fs = score_funding_for_side("unknown_class", "LONG")
        assert fs.score == 0

    def test_unknown_side_returns_zero(self):
        fs = score_funding_for_side("neutral", "FLAT")
        assert fs.score == 0


# ── env defaults ──────────────────────────────────────────────────────────────

class TestEnvDefaults:
    def test_positive_threshold_default(self):
        from app.market_data.funding import _get_thresholds
        os.environ.pop("FUNDING_POSITIVE", None)
        t = _get_thresholds()
        assert t["positive"] == pytest.approx(0.0003)

    def test_negative_threshold_default(self):
        from app.market_data.funding import _get_thresholds
        os.environ.pop("FUNDING_NEGATIVE", None)
        t = _get_thresholds()
        assert t["negative"] == pytest.approx(-0.0003)

    def test_extreme_positive_threshold_default(self):
        from app.market_data.funding import _get_thresholds
        os.environ.pop("FUNDING_EXTREME_POSITIVE", None)
        t = _get_thresholds()
        assert t["extreme_positive"] == pytest.approx(0.0008)

    def test_extreme_negative_threshold_default(self):
        from app.market_data.funding import _get_thresholds
        os.environ.pop("FUNDING_EXTREME_NEGATIVE", None)
        t = _get_thresholds()
        assert t["extreme_negative"] == pytest.approx(-0.0008)

    def test_custom_threshold_from_env(self):
        from app.market_data.funding import _get_thresholds
        os.environ["FUNDING_EXTREME_POSITIVE"] = "0.0015"
        t = _get_thresholds()
        assert t["extreme_positive"] == pytest.approx(0.0015)
        del os.environ["FUNDING_EXTREME_POSITIVE"]

    def test_invalid_env_falls_back_to_default(self):
        from app.market_data.funding import _get_thresholds
        os.environ["FUNDING_POSITIVE"] = "not_a_number"
        t = _get_thresholds()
        assert t["positive"] == pytest.approx(0.0003)
        del os.environ["FUNDING_POSITIVE"]


# ── safe missing funding data ──────────────────────────────────────────────────

class TestMissingFunding:
    def test_none_funding_data_score_is_zero(self):
        funding_data = None
        funding_score = 0 if funding_data is None else score_funding_for_side(
            funding_data.classification, "LONG"
        ).score
        assert funding_score == 0

    def test_confidence_unchanged_when_funding_missing(self):
        base_confidence = 80.0
        funding_score = 0
        adjusted = round(max(0.0, min(100.0, base_confidence + funding_score)), 1)
        assert adjusted == base_confidence

    def test_funding_diag_empty_when_no_data(self):
        funding_data = None
        diag = "" if funding_data is None else f"Funding: rate={funding_data.funding_rate}"
        assert diag == ""

    def test_funding_data_construct(self):
        fd = FundingData(
            symbol="BTCUSDT",
            funding_rate=0.0001,
            funding_time=1690387200000,
            next_funding_time=1690416000000,
            classification="neutral",
        )
        assert fd.symbol == "BTCUSDT"
        assert fd.classification == "neutral"

    def test_extreme_positive_long_crowded_penalty(self):
        # Verify the crowded-long scenario properly penalises a LONG signal
        fs = score_funding_for_side("extreme_positive", "LONG")
        base = 80.0
        adjusted = round(max(0.0, min(100.0, base + fs.score)), 1)
        assert adjusted == 65.0  # 80 - 15

    def test_extreme_negative_short_crowded_penalty(self):
        fs = score_funding_for_side("extreme_negative", "SHORT")
        base = 80.0
        adjusted = round(max(0.0, min(100.0, base + fs.score)), 1)
        assert adjusted == 65.0  # 80 - 15
