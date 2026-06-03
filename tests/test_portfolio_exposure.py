"""Sprint 22A — Portfolio Exposure + Position Lock Engine (pure, no DB)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.risk.portfolio_exposure import (
    ExposureDecision,
    PortfolioExposureState,
    Position,
    build_state,
    calculate_exposure_score,
    can_open_position,
    has_pending_order,
    is_correlated_symbol,
    is_symbol_locked,
)


@pytest.fixture
def engine():
    keys = [
        "portfolio_exposure_engine_enabled",
        "max_open_positions_per_user",
        "max_same_direction_positions",
        "max_correlated_positions",
        "max_daily_loss_percent",
        "symbol_lock_enabled",
        "pending_order_lock_enabled",
        "correlation_groups",
    ]
    saved = {k: getattr(settings, k) for k in keys}
    settings.portfolio_exposure_engine_enabled = True
    settings.max_open_positions_per_user = 5
    settings.max_same_direction_positions = 3
    settings.max_correlated_positions = 2
    settings.max_daily_loss_percent = 5.0
    settings.symbol_lock_enabled = True
    settings.pending_order_lock_enabled = True
    settings.correlation_groups = "BTC:ETH,SOL,DOGE,AVAX;AI:FET,AGIX,OCEAN;MEME:DOGE,SHIB,PEPE"
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _long(sym, status="OPEN"):
    return Position(symbol=sym, side="LONG", notional=100, status=status)


def _short(sym, status="OPEN"):
    return Position(symbol=sym, side="SHORT", notional=100, status=status)


# ── correlation helpers ──────────────────────────────────────────────────────
def test_correlated_same_base(engine):
    assert is_correlated_symbol("BTCUSDT", "BTC") is True


def test_correlated_same_group(engine):
    assert is_correlated_symbol("BTCUSDT", "ETHUSDT") is True
    assert is_correlated_symbol("FETUSDT", "OCEANUSDT") is True


def test_not_correlated_different_group(engine):
    assert is_correlated_symbol("FETUSDT", "BTCUSDT") is False


def test_correlation_empty_config(engine):
    engine.correlation_groups = ""
    assert is_correlated_symbol("BTCUSDT", "ETHUSDT") is False
    assert is_correlated_symbol("BTCUSDT", "BTC") is True  # same base still


# ── disabled engine ──────────────────────────────────────────────────────────
def test_disabled_always_allows(engine):
    engine.portfolio_exposure_engine_enabled = False
    d = can_open_position("BTCUSDT", "LONG", open_positions=[_long("BTCUSDT")])
    assert d.allowed is True
    assert d.enabled is False


def test_disabled_still_reports_score(engine):
    engine.portfolio_exposure_engine_enabled = False
    d = can_open_position("XRPUSDT", "LONG", open_positions=[_long("BTCUSDT"), _long("ETHUSDT")])
    assert d.exposure_score > 0


# ── symbol / pending lock ────────────────────────────────────────────────────
def test_symbol_lock_blocks_open_symbol(engine):
    d = can_open_position("BTCUSDT", "LONG", open_positions=[_long("BTCUSDT")])
    assert d.allowed is False
    assert "already open" in d.reason


def test_symbol_lock_normalises_quote(engine):
    d = can_open_position("BTC", "LONG", open_positions=[_long("BTCUSDT")])
    assert d.allowed is False


def test_pending_order_lock(engine):
    d = can_open_position("ETHUSDT", "LONG", pending_orders=[_long("ETHUSDT", status="PENDING")])
    assert d.allowed is False
    assert "pending" in d.reason


def test_pending_lock_can_be_disabled(engine):
    engine.pending_order_lock_enabled = False
    st = build_state(pending_orders=[_long("ETHUSDT", status="PENDING")])
    assert has_pending_order("ETHUSDT", st) is False


# ── max open positions ───────────────────────────────────────────────────────
def test_max_open_positions(engine):
    opens = [_long(s) for s in ("AAA", "BBB", "CCC", "DDD", "EEE")]
    d = can_open_position("ZZZ", "LONG", open_positions=opens)
    assert d.allowed is False
    assert "max open positions" in d.reason


def test_under_max_open_allows_uncorrelated(engine):
    engine.correlation_groups = ""  # remove correlation so only count matters
    opens = [_short("AAA"), _short("BBB")]
    d = can_open_position("ZZZ", "LONG", open_positions=opens)
    assert d.allowed is True


# ── same-direction limit ─────────────────────────────────────────────────────
def test_same_direction_limit(engine):
    engine.correlation_groups = ""  # isolate the same-direction rule
    opens = [_long("AAA"), _long("BBB"), _long("CCC")]
    d = can_open_position("ZZZ", "LONG", open_positions=opens)
    assert d.allowed is False
    assert "same-direction" in d.reason


def test_opposite_direction_ok(engine):
    engine.correlation_groups = ""
    opens = [_long("AAA"), _long("BBB"), _long("CCC")]
    d = can_open_position("ZZZ", "SHORT", open_positions=opens)
    assert d.allowed is True


# ── correlated limit ─────────────────────────────────────────────────────────
def test_correlated_limit(engine):
    # BTC group LONG x2 already -> third correlated LONG blocked
    opens = [_long("BTCUSDT"), _long("ETHUSDT")]
    d = can_open_position("SOLUSDT", "LONG", open_positions=opens)
    assert d.allowed is False
    assert "correlated" in d.reason
    assert d.correlated_group == "BTC"


def test_correlated_limit_respects_direction(engine):
    opens = [_short("BTCUSDT"), _short("ETHUSDT")]
    # SOL LONG is not correlated *in the same direction* as the two SHORTs
    d = can_open_position("SOLUSDT", "LONG", open_positions=opens)
    assert d.allowed is True


# ── daily loss ───────────────────────────────────────────────────────────────
def test_daily_loss_blocks(engine):
    d = can_open_position("ZZZ", "LONG", daily_pnl_percent=-6.0)
    assert d.allowed is False
    assert "daily loss" in d.reason
    assert d.daily_loss_percent == 6.0


def test_daily_profit_allows(engine):
    d = can_open_position("ZZZ", "LONG", daily_pnl_percent=3.0)
    assert d.allowed is True
    assert d.daily_loss_percent == 0.0


def test_daily_loss_zero_limit_disables(engine):
    engine.max_daily_loss_percent = 0
    d = can_open_position("ZZZ", "LONG", daily_pnl_percent=-50.0)
    assert d.allowed is True


# ── exposure score ───────────────────────────────────────────────────────────
def test_exposure_score_empty():
    assert calculate_exposure_score([]) == 0.0


def test_exposure_score_balanced_low(engine):
    engine.correlation_groups = ""
    s = calculate_exposure_score([_long("A"), _short("B")])
    one_sided = calculate_exposure_score([_long("A"), _long("B")])
    assert s < one_sided


def test_exposure_score_correlated_higher(engine):
    corr = calculate_exposure_score([_long("BTCUSDT"), _long("ETHUSDT"), _long("SOLUSDT")])
    assert corr > 0


def test_exposure_score_capped_100(engine):
    opens = [_long(s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "AVAXUSDT")]
    assert 0 <= calculate_exposure_score(opens) <= 100


# ── state + diagnostics ──────────────────────────────────────────────────────
def test_build_state_counts(engine):
    st = build_state(open_positions=[_long("BTCUSDT"), _short("ETHUSDT")])
    assert st.long_count == 1
    assert st.short_count == 1
    assert st.open_count == 2


def test_state_diagnostics_shape(engine):
    st = build_state(open_positions=[_long("BTCUSDT"), _long("ETHUSDT")])
    d = st.to_diagnostics()
    for key in (
        "exposure_score",
        "open_positions",
        "long_count",
        "short_count",
        "daily_loss_percent",
        "correlation_groups",
        "locked_symbols",
    ):
        assert key in d


def test_decision_diagnostics_shape(engine):
    d = can_open_position("BTCUSDT", "LONG", open_positions=[_long("BTCUSDT")]).to_diagnostics()
    assert d["portfolio_exposure_enabled"] is True
    assert d["portfolio_allowed"] is False
    assert d["portfolio_reject_reason"]


def test_long_short_ratio(engine):
    st = build_state(open_positions=[_long("A"), _long("B"), _short("C")])
    assert st.long_short_ratio == 2.0


def test_accepts_dict_positions(engine):
    d = can_open_position(
        "BTCUSDT",
        "LONG",
        open_positions=[{"symbol": "BTCUSDT", "side": "LONG", "status": "OPEN"}],
    )
    assert d.allowed is False


def test_locked_symbols_explicit(engine):
    st = build_state(locked_symbols=["BTCUSDT"])
    assert is_symbol_locked("BTC", st) is True


def test_clean_portfolio_allows(engine):
    d = can_open_position("BTCUSDT", "LONG", open_positions=[], pending_orders=[])
    assert d.allowed is True
    assert d.reason == "ok"
