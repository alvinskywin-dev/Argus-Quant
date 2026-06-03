"""
Sprint 21F — unit tests for the Binance live/testnet validation layer.

Pure functions only: clock-skew classification, exchangeInfo filter parsing,
quantity/price rounding, min-notional + order-quantity planning, preflight
aggregation, the testnet futures-account classifier, and host selection.
No network and no real orders — the ``run_binance_preflight`` / ``validate_*``
network paths are exercised manually against testnet, never in CI.
"""

from __future__ import annotations

from app.exchange_vault.binance_preflight import (
    BinancePreflightResult,
    SymbolFilters,
    build_preflight_summary,
    check_min_notional,
    classify_clock_skew,
    enforce_order_precision,
    fapi_base,
    parse_symbol_filters,
    plan_order_quantity,
    round_price,
    round_step_down,
    round_step_up,
)
from app.exchange_vault.permission_validator import (
    STATUS_CONNECTED,
    STATUS_PERMISSION_DENIED,
    classify_binance_futures_account,
)

# ── clock skew ──────────────────────────────────────────────────────


def test_clock_skew_within_tolerance_is_ok():
    r = classify_clock_skew(1_000_000, 1_000_100)
    assert r.ok and r.severity == "OK" and r.skew_ms == -100


def test_clock_skew_high_warns_but_passes():
    r = classify_clock_skew(1_000_700, 1_000_000)
    assert r.ok and r.severity == "WARN" and r.skew_ms == 700


def test_clock_skew_beyond_recv_window_fails():
    r = classify_clock_skew(1_002_500, 1_000_000)
    assert not r.ok and r.severity == "FAIL" and r.abs_skew_ms == 2500
    assert "-1021" in r.message


# ── exchangeInfo filter parsing ─────────────────────────────────────

_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "quantityPrecision": 3,
            "pricePrecision": 1,
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.001",
                    "minQty": "0.001",
                    "maxQty": "1000",
                },
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
    ]
}


def test_parse_symbol_filters_reads_lot_price_notional():
    f = parse_symbol_filters(_INFO, "btcusdt")
    assert f.found and f.tradable
    assert f.step_size == 0.001 and f.min_qty == 0.001
    assert f.tick_size == 0.10 and f.min_notional == 5.0
    assert f.qty_precision == 3 and f.price_precision == 1


def test_parse_symbol_filters_missing_symbol():
    f = parse_symbol_filters(_INFO, "ETHUSDT")
    assert not f.found and not f.tradable


def test_parse_symbol_filters_spot_min_notional_key():
    info = {
        "symbols": [
            {
                "symbol": "X",
                "status": "TRADING",
                "filters": [{"filterType": "MIN_NOTIONAL", "minNotional": "10"}],
            }
        ]
    }
    assert parse_symbol_filters(info, "X").min_notional == 10.0


# ── rounding ────────────────────────────────────────────────────────


def test_round_step_down_truncates_to_step():
    assert round_step_down(0.123456, 0.001) == 0.123
    assert round_step_down(1.9999, 0.5) == 1.5
    assert round_step_down(5.0, 0) == 5.0  # no step -> unchanged


def test_round_step_up_bumps_to_step():
    assert round_step_up(0.1231, 0.001) == 0.124
    assert round_step_up(1.1, 0.5) == 1.5


def test_round_price_to_tick_half_up():
    assert round_price(100.07, 0.10) == 100.10
    assert round_price(100.04, 0.10) == 100.00


# ── min-notional + order planning ───────────────────────────────────


def test_check_min_notional():
    ok, notional = check_min_notional(0.001, 4000, 5)
    assert not ok and notional == 4.0
    ok, notional = check_min_notional(0.01, 4000, 5)
    assert ok and notional == 40.0


def test_plan_order_quantity_rounds_and_meets_minimums():
    f = parse_symbol_filters(_INFO, "BTCUSDT")
    plan = plan_order_quantity(f, price=4000.0, target_notional=25.0)
    assert plan.ok
    # 25/4000 = 0.00625 -> step-down to 0.006 -> notional 24 < 5? no, >=5 so kept
    assert plan.qty == 0.006 and abs(plan.notional - 24.0) < 1e-9


def test_plan_order_quantity_bumps_to_min_notional():
    f = parse_symbol_filters(_INFO, "BTCUSDT")
    # target below min-notional should be bumped up to satisfy it
    plan = plan_order_quantity(f, price=4000.0, target_notional=2.0)
    assert plan.ok and plan.notional >= 5.0 and plan.qty >= f.min_qty


def test_plan_order_quantity_unknown_symbol_fails():
    f = SymbolFilters(symbol="NOPE", found=False)
    plan = plan_order_quantity(f, price=10, target_notional=10)
    assert not plan.ok and "not found" in plan.reason


def test_plan_order_quantity_rejects_nonpositive():
    f = parse_symbol_filters(_INFO, "BTCUSDT")
    assert not plan_order_quantity(f, price=0, target_notional=10).ok
    assert not plan_order_quantity(f, price=10, target_notional=0).ok


# ── order precision enforcement (executor path) ─────────────────────


def test_enforce_precision_rounds_qty_down_and_price_to_tick():
    f = parse_symbol_filters(_INFO, "BTCUSDT")
    r = enforce_order_precision(f, qty=0.12345, price=100.07, order_type="LIMIT")
    assert r.ok and r.qty == 0.123 and r.price == 100.10


def test_enforce_precision_market_leaves_price_none():
    f = parse_symbol_filters(_INFO, "BTCUSDT")
    r = enforce_order_precision(f, qty=0.0039, price=None, order_type="MARKET")
    assert r.ok and r.qty == 0.003 and r.price is None


def test_enforce_precision_rejects_below_min_qty():
    f = parse_symbol_filters(_INFO, "BTCUSDT")
    # 0.0005 rounds down to 0.000 -> below min_qty 0.001 -> hard reject
    r = enforce_order_precision(f, qty=0.0005, price=None, order_type="MARKET")
    assert not r.ok and "below minimum" in r.reason


def test_enforce_precision_passthrough_without_filters():
    f = SymbolFilters(symbol="NEW", found=False)
    r = enforce_order_precision(f, qty=1.23456, price=9.99, order_type="LIMIT")
    assert r.ok and r.qty == 1.23456 and r.price == 9.99


# ── preflight aggregation ───────────────────────────────────────────


def test_preflight_summary_and_finalize():
    res = BinancePreflightResult(testnet=True, base_url=fapi_base(True))
    res.add("clock_skew", True, "ok")
    res.add("account_read", True, "ok")
    res.add("balance_positive", True, "ok", severity="WARN")  # WARN still ok
    res.finalize()
    assert res.ok
    assert build_preflight_summary(res.checks)


def test_preflight_fails_on_hard_failure():
    res = BinancePreflightResult(testnet=False)
    res.add("clock_skew", True, "ok")
    res.add("account_read", False, "bad key")
    res.finalize()
    assert not res.ok
    pub = res.to_public_dict()
    assert pub["ok"] is False and any(c["name"] == "account_read" for c in pub["checks"])


def test_empty_preflight_is_not_ok():
    assert not build_preflight_summary([])


# ── host selection ──────────────────────────────────────────────────


def test_fapi_base_selects_testnet_vs_prod():
    assert "testnet" in fapi_base(True)
    assert fapi_base(False) == "https://fapi.binance.com"


# ── testnet futures-account classifier ──────────────────────────────


def test_futures_account_cantrade_true_is_connected():
    r = classify_binance_futures_account(
        {"canTrade": True, "canWithdraw": False, "totalWalletBalance": "100"}, testnet=True
    )
    assert r.ok and r.status == STATUS_CONNECTED
    assert r.can_read and r.can_trade and r.can_futures
    # account-level withdraw flag is not trusted as key-permission -> undetectable
    assert r.can_withdraw is None and "Withdrawal" in r.permission_warning
    assert r.account_type == "futures-testnet"


def test_futures_account_cantrade_false_is_denied():
    r = classify_binance_futures_account({"canTrade": False, "totalWalletBalance": "0"})
    assert r.status == STATUS_PERMISSION_DENIED and not r.can_trade


def test_futures_account_unreachable_is_denied():
    r = classify_binance_futures_account({}, testnet=True)
    assert not r.ok and r.status == STATUS_PERMISSION_DENIED


def test_futures_account_trusted_withdraw_flag_rejects():
    r = classify_binance_futures_account(
        {"canTrade": True, "canWithdraw": True}, trust_withdraw_flag=True
    )
    assert r.status == STATUS_PERMISSION_DENIED and r.can_withdraw is True
