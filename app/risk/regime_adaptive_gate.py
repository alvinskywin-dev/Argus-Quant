"""
Regime Adaptive Gate V1.

Adjusts the three gate thresholds — minimum risk/reward, maximum stop-loss
distance, and minimum confidence — to the prevailing market regime. In
LOW_VOLATILITY / range markets the StopLoss V2 1D support/resistance sits far
from a 15m entry, inflating SL distance and failing RR on every setup; this gate
relaxes those thresholds (within hard clamps) so valid setups can pass, while
TIGHTENING them in HIGH_VOLATILITY / SIDEWAYS.

It NEVER forces a signal: it only changes the numbers the existing checks
compare against. With the feature flag off it returns the base thresholds
unchanged, so behaviour is identical to before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config import settings

# Hard clamps — the adapted thresholds may never cross these, whatever the
# per-regime config says.
HARD_MIN_RR = 1.0
HARD_MAX_SL_DISTANCE_PERCENT = 20.0
HARD_MIN_CONFIDENCE = 70.0


@dataclass
class RegimeAdaptiveThresholds:
    market_regime: str
    effective_min_rr: float
    effective_max_sl_distance_percent: float
    effective_min_confidence: float
    confidence_delta: int
    reason: str
    # Carried for diagnostics / API.
    enabled: bool = False
    base_min_rr: float = 0.0
    base_max_sl_distance_percent: float = 0.0
    base_min_confidence: float = 0.0

    def to_diagnostics(self) -> dict:
        return {
            "regime_adaptive_enabled": self.enabled,
            "market_regime": self.market_regime,
            "base_min_rr": round(self.base_min_rr, 4),
            "effective_min_rr": round(self.effective_min_rr, 4),
            "base_max_sl_distance_percent": round(self.base_max_sl_distance_percent, 4),
            "effective_max_sl_distance_percent": round(self.effective_max_sl_distance_percent, 4),
            "base_min_confidence": round(self.base_min_confidence, 4),
            "effective_min_confidence": round(self.effective_min_confidence, 4),
            "regime_threshold_reason": self.reason,
        }


# regime -> (min_rr setting, max_sl setting, confidence_delta setting, reason)
def _regime_params(regime: str) -> tuple[float, float, int, str]:
    r = (regime or "").upper()
    if r == "LOW_VOLATILITY":
        return (
            settings.low_vol_min_rr,
            settings.low_vol_max_sl_distance_percent,
            settings.low_vol_min_confidence_delta,
            "LOW_VOLATILITY: relaxed RR and SL distance",
        )
    if r == "HIGH_VOLATILITY":
        return (
            settings.high_vol_min_rr,
            settings.high_vol_max_sl_distance_percent,
            settings.high_vol_min_confidence_delta,
            "HIGH_VOLATILITY: tightened RR, SL distance and confidence",
        )
    if r == "SIDEWAYS":
        return (
            settings.sideways_min_rr,
            settings.sideways_max_sl_distance_percent,
            settings.sideways_min_confidence_delta,
            "SIDEWAYS: tightened thresholds",
        )
    if r == "BULL":
        return (
            settings.bull_min_rr,
            settings.bull_max_sl_distance_percent,
            settings.bull_min_confidence_delta,
            "BULL: moderately relaxed thresholds",
        )
    if r == "BEAR":
        return (
            settings.bear_min_rr,
            settings.bear_max_sl_distance_percent,
            settings.bear_min_confidence_delta,
            "BEAR: moderately relaxed thresholds",
        )
    # NORMAL / unknown / None → the NORMAL baseline.
    return (
        settings.normal_min_rr,
        settings.normal_max_sl_distance_percent,
        settings.normal_min_confidence_delta,
        "NORMAL: baseline thresholds",
    )


def get_effective_thresholds(
    base_min_rr: float,
    base_max_sl_distance_percent: float,
    base_min_confidence: float,
    market_regime: Optional[str],
) -> RegimeAdaptiveThresholds:
    """Return the regime-adjusted thresholds (or the base ones when the gate is
    disabled). All outputs are hard-clamped."""
    regime_name = (market_regime or "UNKNOWN").upper()

    if not settings.regime_adaptive_gate_enabled:
        return RegimeAdaptiveThresholds(
            market_regime=regime_name,
            effective_min_rr=base_min_rr,
            effective_max_sl_distance_percent=base_max_sl_distance_percent,
            effective_min_confidence=base_min_confidence,
            confidence_delta=0,
            reason="adaptive gate disabled — base thresholds",
            enabled=False,
            base_min_rr=base_min_rr,
            base_max_sl_distance_percent=base_max_sl_distance_percent,
            base_min_confidence=base_min_confidence,
        )

    min_rr, max_sl, conf_delta, reason = _regime_params(regime_name)
    eff_min_rr = max(HARD_MIN_RR, float(min_rr))
    eff_max_sl = min(HARD_MAX_SL_DISTANCE_PERCENT, float(max_sl))
    eff_min_conf = max(HARD_MIN_CONFIDENCE, float(base_min_confidence) + int(conf_delta))

    return RegimeAdaptiveThresholds(
        market_regime=regime_name,
        effective_min_rr=eff_min_rr,
        effective_max_sl_distance_percent=eff_max_sl,
        effective_min_confidence=eff_min_conf,
        confidence_delta=int(conf_delta),
        reason=reason,
        enabled=True,
        base_min_rr=base_min_rr,
        base_max_sl_distance_percent=base_max_sl_distance_percent,
        base_min_confidence=base_min_confidence,
    )
