"""
Sprint 21D — order failure classification + retry policy (pure logic).

This is the safety brain of live execution resilience. It decides, for a given
failure, whether a retry is allowed, after how long, and whether the order's
state must be reconciled with the exchange BEFORE any further action (so we
never blindly resend an order whose true state we don't know).

All functions here are pure and fully unit-tested. The service layer applies the
decision; it is the only place that touches the DB or the exchange.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# ── error classes ───────────────────────────────────────────────────
INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
PRECISION_ERROR = "PRECISION_ERROR"
MIN_NOTIONAL = "MIN_NOTIONAL"
RATE_LIMIT = "RATE_LIMIT"
NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
EXCHANGE_DOWN = "EXCHANGE_DOWN"
ORDER_REJECTED = "ORDER_REJECTED"
REDUCE_ONLY_REJECTED = "REDUCE_ONLY_REJECTED"
TP_SL_FAILED = "TP_SL_FAILED"
UNKNOWN = "UNKNOWN"

ALL_ERROR_CLASSES = (
    INSUFFICIENT_BALANCE,
    PRECISION_ERROR,
    MIN_NOTIONAL,
    RATE_LIMIT,
    NETWORK_TIMEOUT,
    EXCHANGE_DOWN,
    ORDER_REJECTED,
    REDUCE_ONLY_REJECTED,
    TP_SL_FAILED,
    UNKNOWN,
)

# ── final states ────────────────────────────────────────────────────
PENDING = "PENDING"
RETRY_SCHEDULED = "RETRY_SCHEDULED"
NEEDS_RECONCILE = "NEEDS_RECONCILE"
RESOLVED = "RESOLVED"
FAILED = "FAILED"


def classify_error(message: str, *, code: str | int | None = None, is_tp_sl: bool = False) -> str:
    """
    Classify an exchange error from its message (+ optional code) into one of the
    error classes. ``is_tp_sl`` lets the caller flag a protective-order failure.
    """
    msg = (message or "").lower()
    code = str(code) if code is not None else ""

    # Binance-style numeric codes (most precise signal).
    if code in ("-2019", "-2018"):  # margin is insufficient / balance
        return INSUFFICIENT_BALANCE
    if code in ("-1111", "-4014"):  # precision over maximum / price precision
        return PRECISION_ERROR
    if code in ("-4164", "-1013"):  # notional must be >= min / filter failure
        return MIN_NOTIONAL
    if code in ("-1003", "-1015", "429"):  # too many requests / rate limit
        return RATE_LIMIT
    if code in ("-2022", "-2021"):  # reduceOnly rejected / order would not reduce
        return REDUCE_ONLY_REJECTED

    if "insufficient" in msg and ("balance" in msg or "margin" in msg):
        return INSUFFICIENT_BALANCE
    if "reduceonly" in msg or "reduce-only" in msg or "reduce only" in msg:
        return REDUCE_ONLY_REJECTED
    if "precision" in msg or "step size" in msg or "lot size" in msg:
        return PRECISION_ERROR
    if "notional" in msg or "min_notional" in msg:
        return MIN_NOTIONAL
    if "rate limit" in msg or "too many request" in msg or "-1003" in msg or "429" in msg:
        return RATE_LIMIT
    if "timeout" in msg or "timed out" in msg:
        return NETWORK_TIMEOUT
    if (
        "unavailable" in msg
        or "service" in msg
        and "down" in msg
        or "503" in msg
        or "502" in msg
        or "system busy" in msg
        or "maintenance" in msg
    ):
        return EXCHANGE_DOWN
    if is_tp_sl:
        return TP_SL_FAILED
    if "reject" in msg or "would immediately trigger" in msg:
        return ORDER_REJECTED
    return UNKNOWN


@dataclass
class RetryDecision:
    should_retry: bool
    delay_sec: float
    terminal: bool  # no further attempts will ever be made
    needs_reconcile: bool  # verify true order state before acting again
    final_state: str
    reason: str


def backoff_delay(retry_count: int, *, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff: base * 2**retry_count, capped."""
    return float(min(cap, base * (2 ** max(0, retry_count))))


def decide_retry(
    error_class: str,
    retry_count: int,
    *,
    max_retries: int = 5,
    recommended_delay: float | None = None,
) -> RetryDecision:
    """Given the error class and how many retries already happened, decide next."""

    # Terminal, never-retry classes — surface to the user, don't loop.
    if error_class == INSUFFICIENT_BALANCE:
        return RetryDecision(
            False, 0.0, True, False, FAILED, "Insufficient balance — notify user, do not retry."
        )
    if error_class == MIN_NOTIONAL:
        return RetryDecision(
            False, 0.0, True, False, FAILED, "Below minimum notional — reject, do not retry."
        )

    # Precision: round to step size and retry exactly once.
    if error_class == PRECISION_ERROR:
        if retry_count < 1:
            return RetryDecision(
                True,
                0.0,
                False,
                False,
                RETRY_SCHEDULED,
                "Round to exchange step size and retry once.",
            )
        return RetryDecision(False, 0.0, True, False, FAILED, "Precision retry exhausted.")

    # Reduce-only rejection means our view of the position is wrong — reconcile.
    if error_class == REDUCE_ONLY_REJECTED:
        return RetryDecision(
            False,
            0.0,
            False,
            True,
            NEEDS_RECONCILE,
            "Reduce-only rejected — reconcile position before retry.",
        )

    # Plain rejection / unknown: do NOT blindly resend; verify first.
    if error_class == ORDER_REJECTED:
        return RetryDecision(
            False, 0.0, True, False, FAILED, "Order rejected — manual review, no blind retry."
        )
    if error_class == UNKNOWN:
        return RetryDecision(
            False,
            0.0,
            False,
            True,
            NEEDS_RECONCILE,
            "Unknown failure — verify order status before any retry.",
        )

    # Retryable transient classes share the budget check.
    if retry_count >= max_retries:
        return RetryDecision(
            False,
            0.0,
            True,
            False,
            FAILED,
            f"Retry budget exhausted ({retry_count}/{max_retries}).",
        )

    if error_class == RATE_LIMIT:
        delay = recommended_delay if recommended_delay is not None else 2.0
        return RetryDecision(
            True,
            float(delay),
            False,
            False,
            RETRY_SCHEDULED,
            "Rate limited — retry after recommended delay.",
        )
    if error_class == NETWORK_TIMEOUT:
        # Unknown execution state: verify on the exchange, then retry with backoff.
        return RetryDecision(
            True,
            backoff_delay(retry_count),
            False,
            True,
            NEEDS_RECONCILE,
            "Timeout — check order status, then retry with backoff.",
        )
    if error_class == EXCHANGE_DOWN:
        return RetryDecision(
            True,
            backoff_delay(retry_count, base=5.0, cap=120.0),
            False,
            False,
            RETRY_SCHEDULED,
            "Exchange down — retry with longer backoff.",
        )
    if error_class == TP_SL_FAILED:
        # Retry immediately first, then with backoff — leaving a position
        # unprotected is the worst case, so we are aggressive but bounded.
        delay = 0.0 if retry_count == 0 else backoff_delay(retry_count)
        return RetryDecision(
            True,
            delay,
            False,
            False,
            RETRY_SCHEDULED,
            "TP/SL failed — retry immediately then with backoff.",
        )

    return RetryDecision(
        False, 0.0, False, True, NEEDS_RECONCILE, "Unclassified — reconcile before retry."
    )


def idempotency_key(
    *, user_id: int, exchange: str, symbol: str, side: str, signal_id: int | str | None = None
) -> str:
    """
    Stable key for a logical live entry so the same signal can never open two
    live positions for the same user/exchange/symbol/side across retries.
    """
    raw = f"{user_id}:{exchange.lower()}:{symbol.upper()}:{side.upper()}:{signal_id if signal_id is not None else 'na'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def breaker_tripped(
    failure_times: list[float], now: float, *, window_sec: int, threshold: int
) -> bool:
    """
    Circuit breaker: True when at least ``threshold`` failures occurred within
    the trailing ``window_sec`` seconds. threshold<=0 disables the breaker.
    """
    if threshold <= 0:
        return False
    recent = [t for t in failure_times if 0 <= (now - t) <= window_sec]
    return len(recent) >= threshold
