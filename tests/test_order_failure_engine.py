"""
Sprint 21D — unit tests for the pure order-failure classification + retry policy.

The DB service (record_failure, circuit_breaker_tripped) is exercised manually;
the safety-critical decisions — how to classify an error and whether/when to
retry — are pure and fully covered here.
"""
from __future__ import annotations

from app.order_failures import policy as p


# ── classification ─────────────────────────────────────────────────

def test_classify_insufficient_balance():
    assert p.classify_error("Account has insufficient balance for requested action") == p.INSUFFICIENT_BALANCE
    assert p.classify_error("margin is insufficient", code=-2019) == p.INSUFFICIENT_BALANCE


def test_classify_precision():
    assert p.classify_error("Precision is over the maximum defined for this asset") == p.PRECISION_ERROR
    assert p.classify_error("LOT_SIZE / step size", code=-1111) == p.PRECISION_ERROR


def test_classify_min_notional():
    assert p.classify_error("Order's notional must be no smaller than 5") == p.MIN_NOTIONAL
    assert p.classify_error("filter failure", code=-4164) == p.MIN_NOTIONAL


def test_classify_rate_limit():
    assert p.classify_error("Too many requests; current limit is ...", code=-1003) == p.RATE_LIMIT
    assert p.classify_error("429 rate limit") == p.RATE_LIMIT


def test_classify_network_and_exchange_down():
    assert p.classify_error("Read timed out") == p.NETWORK_TIMEOUT
    assert p.classify_error("Service unavailable, system busy") == p.EXCHANGE_DOWN


def test_classify_reduce_only_and_rejected():
    assert p.classify_error("ReduceOnly Order is rejected", code=-2022) == p.REDUCE_ONLY_REJECTED
    assert p.classify_error("Order would immediately trigger") == p.ORDER_REJECTED


def test_classify_tp_sl_flag_and_unknown():
    assert p.classify_error("some odd error", is_tp_sl=True) == p.TP_SL_FAILED
    assert p.classify_error("totally novel message") == p.UNKNOWN


# ── retry policy ────────────────────────────────────────────────────

def test_insufficient_balance_is_terminal_no_retry():
    d = p.decide_retry(p.INSUFFICIENT_BALANCE, 0)
    assert not d.should_retry and d.terminal and d.final_state == p.FAILED


def test_min_notional_is_terminal():
    d = p.decide_retry(p.MIN_NOTIONAL, 0)
    assert not d.should_retry and d.terminal


def test_precision_retries_once_then_terminal():
    first = p.decide_retry(p.PRECISION_ERROR, 0)
    second = p.decide_retry(p.PRECISION_ERROR, 1)
    assert first.should_retry and first.delay_sec == 0.0
    assert not second.should_retry and second.terminal


def test_rate_limit_uses_recommended_delay():
    d = p.decide_retry(p.RATE_LIMIT, 0, recommended_delay=7.5)
    assert d.should_retry and d.delay_sec == 7.5


def test_network_timeout_needs_reconcile_and_backs_off():
    d0 = p.decide_retry(p.NETWORK_TIMEOUT, 0)
    d2 = p.decide_retry(p.NETWORK_TIMEOUT, 2)
    assert d0.should_retry and d0.needs_reconcile
    assert d2.delay_sec > d0.delay_sec   # exponential backoff


def test_reduce_only_requires_reconcile_no_blind_retry():
    d = p.decide_retry(p.REDUCE_ONLY_REJECTED, 0)
    assert not d.should_retry and d.needs_reconcile and d.final_state == p.NEEDS_RECONCILE


def test_unknown_does_not_blindly_retry():
    d = p.decide_retry(p.UNKNOWN, 0)
    assert not d.should_retry and d.needs_reconcile


def test_tp_sl_retries_immediately_first():
    d0 = p.decide_retry(p.TP_SL_FAILED, 0)
    d1 = p.decide_retry(p.TP_SL_FAILED, 1)
    assert d0.should_retry and d0.delay_sec == 0.0
    assert d1.should_retry and d1.delay_sec > 0.0


def test_retry_budget_exhausted_is_terminal():
    d = p.decide_retry(p.NETWORK_TIMEOUT, 5, max_retries=5)
    assert not d.should_retry and d.terminal


def test_backoff_is_capped():
    assert p.backoff_delay(100) == 60.0
    assert p.backoff_delay(0) == 1.0
    assert p.backoff_delay(3) == 8.0


# ── idempotency + circuit breaker ───────────────────────────────────

def test_idempotency_key_is_stable_and_distinct():
    a = p.idempotency_key(user_id=1, exchange="binance", symbol="BTCUSDT", side="LONG", signal_id=42)
    b = p.idempotency_key(user_id=1, exchange="BINANCE", symbol="btcusdt", side="long", signal_id=42)
    c = p.idempotency_key(user_id=1, exchange="binance", symbol="BTCUSDT", side="SHORT", signal_id=42)
    assert a == b          # case-insensitive, stable
    assert a != c          # different side -> different key


def test_circuit_breaker_trips_within_window():
    now = 1000.0
    times = [995.0, 996.0, 997.0, 998.0, 999.0]  # 5 in last 5s
    assert p.breaker_tripped(times, now, window_sec=300, threshold=5)
    assert not p.breaker_tripped(times[:4], now, window_sec=300, threshold=5)


def test_circuit_breaker_ignores_old_failures():
    now = 1000.0
    times = [100.0, 200.0, 300.0, 400.0, 999.0]  # only 1 inside 300s window
    assert not p.breaker_tripped(times, now, window_sec=300, threshold=5)


def test_circuit_breaker_disabled_when_threshold_zero():
    assert not p.breaker_tripped([1, 2, 3], 3, window_sec=300, threshold=0)
