"""
Short Protection Layer — Sprint 19B.

Rejects low-quality SHORT signals via 5 sequential filters:

    #1  Bull regime    — if BULL, SHORT needs conf >= min_confidence + 8
    #2  Funding        — positive funding + price above EMA200 → reject
    #3  Open Interest  — price rising + OI rising → reject
    #4  Liquidity      — no bearish sweep AND liquidity_score < 8 → reject
    #5  Trend align    — both 15m and 1h must be bearish

Rejection counts are tracked in Redis for the analytics endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import settings
from app.market_data.cache import cache_get, cache_set
from app.utils.logger import logger

_STATS_KEY = "short_protection:stats:v1"
_STATS_TTL = 86400 * 7  # rolling 7-day window


@dataclass
class ShortProtectionResult:
    passed: bool
    rejection_reason: Optional[str]


# ── Redis stat helpers ────────────────────────────────────────────────────────

async def _increment_stat(field: str, amount: int = 1) -> None:
    try:
        stats: Dict[str, int] = await cache_get(_STATS_KEY) or {}
        stats[field] = int(stats.get(field, 0)) + amount
        await cache_set(_STATS_KEY, stats, ttl=_STATS_TTL)
    except Exception:
        pass


async def get_short_protection_stats() -> Dict[str, Any]:
    """Return aggregated short protection statistics for the analytics endpoint."""
    try:
        stats: Dict[str, int] = await cache_get(_STATS_KEY) or {}
        candidates = int(stats.get("candidates", 0))
        rejected = int(stats.get("rejected", 0))
        reasons = {
            "Bull regime":    int(stats.get("reject_bull_regime", 0)),
            "Funding":        int(stats.get("reject_funding", 0)),
            "OI":             int(stats.get("reject_oi", 0)),
            "Liquidity":      int(stats.get("reject_liquidity", 0)),
            "Trend mismatch": int(stats.get("reject_trend", 0)),
        }
        top_reason = max(reasons, key=lambda k: reasons[k]) if any(reasons.values()) else None
        rejection_rate = round(rejected / max(1, candidates) * 100, 1)
        return {
            "short_candidates": candidates,
            "short_rejected": rejected,
            "rejection_rate": rejection_rate,
            "top_reason": top_reason,
            "reasons": reasons,
        }
    except Exception as exc:
        logger.debug(f"short protection stats error: {exc}")
        return {
            "short_candidates": 0,
            "short_rejected": 0,
            "rejection_rate": 0.0,
            "top_reason": None,
            "reasons": {},
        }


# ── core filter logic (synchronous) ──────────────────────────────────────────

def check_short_protection(
    snaps: dict,
    regime: Optional[Any],         # MarketRegime | None
    funding_class: Optional[str],
    oi_snap: Optional[Any],        # OISnapshot | None
    liquidity_score: int,
    adjusted_confidence: float,
) -> ShortProtectionResult:
    """
    Run all 5 SHORT protection filters.
    Returns immediately on the first failure with the rejection reason.
    LONG signals are never passed here — use apply_short_protection().
    """
    snap_1d  = snaps.get("1d")
    snap_1h  = snaps.get("1h")
    snap_15m = snaps.get("15m")

    # Filter #1: Bull regime — SHORT needs meaningfully higher confidence
    if regime is not None and regime.market_regime == "BULL":
        required = settings.min_confidence + 8.0
        if adjusted_confidence < required:
            return ShortProtectionResult(
                passed=False,
                rejection_reason=(
                    f"Bull regime: conf {adjusted_confidence:.0f} < required {required:.0f}"
                ),
            )

    # Filter #2: Positive funding + price above EMA200 confirms bullish bias
    if (
        funding_class in ("positive", "extreme_positive")
        and snap_1d is not None
        and snap_1d.last_close > snap_1d.ema_200
    ):
        return ShortProtectionResult(
            passed=False,
            rejection_reason="Bull regime + positive funding",
        )

    # Filter #3: Rising price with rising OI confirms long momentum — reject SHORT
    if (
        oi_snap is not None
        and oi_snap.price_change_pct > 0
        and oi_snap.oi_change_15m > 0
    ):
        return ShortProtectionResult(
            passed=False,
            rejection_reason="Price rising + OI rising",
        )

    # Filter #4: No bearish liquidity sweep and low liquidity score
    has_bearish_sweep = bool(
        snap_15m is not None
        and snap_15m.structure is not None
        and snap_15m.structure.sweep_bear
    )
    if not has_bearish_sweep and liquidity_score < 8:
        return ShortProtectionResult(
            passed=False,
            rejection_reason="No bearish liquidity sweep (liquidity_score < 8)",
        )

    # Filter #5: Trend alignment — both 15m and 1h must be bearish
    m15_bearish = (
        snap_15m is not None
        and snap_15m.ema_fast < snap_15m.ema_slow
        and snap_15m.last_close < snap_15m.ema_slow
    )
    h1_bearish = (
        snap_1h is not None
        and snap_1h.ema_fast < snap_1h.ema_slow
        and snap_1h.last_close < snap_1h.ema_slow
    )
    if not (m15_bearish and h1_bearish):
        return ShortProtectionResult(
            passed=False,
            rejection_reason="Trend mismatch: need 15m+1h bearish alignment",
        )

    return ShortProtectionResult(passed=True, rejection_reason=None)


# ── async wrapper with stats tracking ────────────────────────────────────────

async def apply_short_protection(
    side: str,
    snaps: dict,
    regime: Optional[Any],
    funding_class: Optional[str],
    oi_snap: Optional[Any],
    liquidity_score: int,
    adjusted_confidence: float,
) -> ShortProtectionResult:
    """
    Wrapper around check_short_protection that records statistics.
    LONG signals are returned as passed without recording.
    """
    if side != "SHORT":
        return ShortProtectionResult(passed=True, rejection_reason=None)

    await _increment_stat("candidates")

    result = check_short_protection(
        snaps=snaps,
        regime=regime,
        funding_class=funding_class,
        oi_snap=oi_snap,
        liquidity_score=liquidity_score,
        adjusted_confidence=adjusted_confidence,
    )

    if not result.passed:
        await _increment_stat("rejected")
        reason = result.rejection_reason or ""
        # Map to the appropriate stat bucket
        if "Bull regime" in reason and "funding" not in reason.lower():
            await _increment_stat("reject_bull_regime")
        elif "funding" in reason.lower():
            await _increment_stat("reject_funding")
        elif "OI" in reason or "rising" in reason:
            await _increment_stat("reject_oi")
        elif "liquidity" in reason.lower() or "sweep" in reason.lower():
            await _increment_stat("reject_liquidity")
        elif "Trend" in reason or "bearish" in reason.lower():
            await _increment_stat("reject_trend")

    return result
