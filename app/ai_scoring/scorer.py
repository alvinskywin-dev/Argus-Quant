"""
AI-weighted signal scoring.

The "AI" here is a deterministic, explainable, weighted scoring model rather
than a black-box neural net. In production this is the right approach for a
signals product:

- Each feature contributes a normalized 0..1 component score with a weight.
- Confidence = weighted sum, mapped to 0..100.
- Risk level is derived from volatility + structure quality.
- Every contributing component is recorded as a human-readable reason.

If you later want to swap in an ML model, the inputs are already feature
vectors — just plug in a sklearn/torch model that returns a probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.strategies.features import FeatureSnapshot


# Weights sum to roughly 1.0 — tunable.
WEIGHTS: Dict[str, float] = {
    "trend_quality": 0.20,
    "momentum": 0.14,
    "volume_quality": 0.12,
    "volatility_health": 0.08,
    "structure": 0.14,        # SMC: BOS / sweeps / MSS — heaviest
    "supertrend_align": 0.08,
    "macd_alignment": 0.08,
    "vwap_align": 0.06,
    "mtf_confirmation": 0.14,  # filled in by aggregator across timeframes
}


@dataclass
class Score:
    side: str  # LONG or SHORT
    confidence: float                # 0..100
    components: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    risk_level: str = "MEDIUM"
    fake_breakout_prob: float = 0.0


# -------------- helpers --------------
def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _trend_component(snap: FeatureSnapshot, side: str) -> Tuple[float, str | None]:
    # 0..1 based on EMA alignment + ADX strength
    aligned = (
        side == "LONG" and snap.ema_fast > snap.ema_slow and snap.last_close > snap.ema_200
    ) or (
        side == "SHORT" and snap.ema_fast < snap.ema_slow and snap.last_close < snap.ema_200
    )
    if not aligned:
        return 0.0, None
    strength = _clip01((snap.trend_strength_adx - 15) / 35)  # ADX 15->50 maps 0->1
    label = "EMA stack aligned" if strength > 0.3 else None
    return strength, label


def _momentum_component(snap: FeatureSnapshot, side: str) -> Tuple[float, str | None]:
    if side == "LONG":
        if not snap.momentum_bull:
            return 0.0, None
        rsi_score = _clip01((snap.rsi_value - 45) / 25)   # 45..70 ideal
        cross = 0.4 if snap.macd_cross_up else 0.0
        sk = 0.2 if snap.stoch_k > snap.stoch_d else 0.0
        score = min(1.0, rsi_score * 0.6 + cross + sk)
    else:
        if not snap.momentum_bear:
            return 0.0, None
        rsi_score = _clip01((55 - snap.rsi_value) / 25)
        cross = 0.4 if snap.macd_cross_down else 0.0
        sk = 0.2 if snap.stoch_k < snap.stoch_d else 0.0
        score = min(1.0, rsi_score * 0.6 + cross + sk)
    label = "Momentum confirmed" if score > 0.4 else None
    return score, label


def _volume_component(snap: FeatureSnapshot) -> Tuple[float, str | None]:
    vs = snap.vol_spike_pct
    if vs >= 200:
        return 1.0, f"Volume spike +{vs:.0f}%"
    if vs >= 80:
        return 0.75, f"Volume spike +{vs:.0f}%"
    if vs >= 30:
        return 0.5, f"Volume rising +{vs:.0f}%"
    if vs <= -40:
        return 0.0, None  # weak participation
    return 0.25, None


def _volatility_component(snap: FeatureSnapshot) -> Tuple[float, str | None]:
    # We want HEALTHY volatility — not too dead, not too explosive
    atr_pct = snap.atr_pct
    if atr_pct < 0.2:
        return 0.1, None   # dead market
    if atr_pct > 6.0:
        return 0.2, None   # too violent
    if 0.4 <= atr_pct <= 3.0:
        return 1.0, "Healthy volatility"
    return 0.6, None


def _structure_component(snap: FeatureSnapshot, side: str) -> Tuple[float, str | None]:
    s = snap.structure
    score = 0.0
    reason = None
    if side == "LONG":
        if s.bos_bull:
            score = max(score, 0.85)
            reason = "Break of Structure (bull)"
        if s.mss_bull:
            score = max(score, 1.0)
            reason = "Market Structure Shift (bull)"
        if s.sweep_bull:
            score = max(score, 0.9)
            reason = "Bullish liquidity sweep"
    else:
        if s.bos_bear:
            score = max(score, 0.85)
            reason = "Break of Structure (bear)"
        if s.mss_bear:
            score = max(score, 1.0)
            reason = "Market Structure Shift (bear)"
        if s.sweep_bear:
            score = max(score, 0.9)
            reason = "Bearish liquidity sweep"
    return score, reason


def _supertrend_component(snap: FeatureSnapshot, side: str) -> Tuple[float, str | None]:
    aligned = (side == "LONG" and snap.supertrend_dir == 1) or (
        side == "SHORT" and snap.supertrend_dir == -1
    )
    return (1.0, "Supertrend aligned") if aligned else (0.0, None)


def _macd_alignment(snap: FeatureSnapshot, side: str) -> Tuple[float, str | None]:
    if side == "LONG":
        if snap.macd_cross_up:
            return 1.0, "MACD bullish cross"
        if snap.macd_hist > 0:
            return 0.5, None
    else:
        if snap.macd_cross_down:
            return 1.0, "MACD bearish cross"
        if snap.macd_hist < 0:
            return 0.5, None
    return 0.0, None


def _vwap_component(snap: FeatureSnapshot, side: str) -> Tuple[float, str | None]:
    if side == "LONG":
        return (1.0, "Above VWAP") if snap.above_vwap else (0.0, None)
    return (1.0, "Below VWAP") if not snap.above_vwap else (0.0, None)


# ---------------- fake breakout detector ----------------
def fake_breakout_probability(snap: FeatureSnapshot, side: str) -> float:
    """
    Rough heuristic: a breakout candle into the extreme of the Bollinger Band
    on FALLING volume + already extended RSI = suspicious.
    """
    p = 0.0
    if side == "LONG":
        if snap.bb_pos > 0.96 and snap.vol_spike_pct < 30:
            p += 0.35
        if snap.rsi_value > 78 and snap.vol_spike_pct < 50:
            p += 0.25
        if snap.overextended_long:
            p += 0.3
    else:
        if snap.bb_pos < 0.04 and snap.vol_spike_pct < 30:
            p += 0.35
        if snap.rsi_value < 22 and snap.vol_spike_pct < 50:
            p += 0.25
        if snap.overextended_short:
            p += 0.3
    return min(1.0, p)


def _risk_classify(snap: FeatureSnapshot) -> str:
    if snap.atr_pct >= 4.0 or snap.bb_width >= 0.18:
        return "HIGH"
    if snap.atr_pct <= 1.0 and snap.bb_width <= 0.05:
        return "LOW"
    return "MEDIUM"


# ---------------- main scoring ----------------
def score_side(snap: FeatureSnapshot, side: str) -> Score:
    components: Dict[str, float] = {}
    reasons: List[str] = []

    pairs: List[Tuple[str, Tuple[float, Optional[str]]]] = [
        ("trend_quality", _trend_component(snap, side)),
        ("momentum", _momentum_component(snap, side)),
        ("volume_quality", _volume_component(snap)),
        ("volatility_health", _volatility_component(snap)),
        ("structure", _structure_component(snap, side)),
        ("supertrend_align", _supertrend_component(snap, side)),
        ("macd_alignment", _macd_alignment(snap, side)),
        ("vwap_align", _vwap_component(snap, side)),
    ]
    for k, (v, lbl) in pairs:
        components[k] = v
        if lbl:
            reasons.append(lbl)

    # mtf_confirmation is filled by the aggregator later — start at neutral.
    components["mtf_confirmation"] = 0.5

    raw = sum(WEIGHTS[k] * components[k] for k in WEIGHTS)
    confidence = round(raw * 100.0, 1)

    fake_p = fake_breakout_probability(snap, side)

    # Premium AI Scoring V2 penalties:
    # 1) fake breakout
    # 2) choppy/ranging market
    # 3) overextended late entries
    # 4) weak volume confirmation
    if fake_p >= 0.5:
        confidence *= 1 - 0.4 * fake_p
        reasons.append(f"Caution: fake-breakout prob {fake_p:.0%}")

    if snap.bb_width < 0.025 and snap.trend_strength_adx < 20:
        confidence *= 0.72
        reasons.append("Rejected pressure: choppy/ranging market")

    if snap.vol_spike_pct < 20:
        confidence *= 0.86
        reasons.append("Weak volume confirmation")

    if side == "LONG" and snap.overextended_long:
        confidence *= 0.70
        reasons.append("Long overextension penalty")

    if side == "SHORT" and snap.overextended_short:
        confidence *= 0.70
        reasons.append("Short overextension penalty")

    # Make 90+ harder to reach. This improves premium filtering quality.
    if confidence > 88:
        confidence = 88 + (confidence - 88) * 0.55

    return Score(
        side=side,
        confidence=round(confidence, 1),
        components=components,
        reasons=reasons,
        risk_level=_risk_classify(snap),
        fake_breakout_prob=round(fake_p, 3),
    )


def score_both_sides(snap: FeatureSnapshot) -> Score:
    """Return the better of LONG/SHORT."""
    long_s = score_side(snap, "LONG")
    short_s = score_side(snap, "SHORT")
    return long_s if long_s.confidence >= short_s.confidence else short_s
