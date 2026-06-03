"""
Signal Quality Scorer.

For each signal provides a detailed breakdown of component scores:
  - Trend score     (0-20): 1D EMA alignment and structure
  - Structure score (0-20): 4H confluence quality
  - Setup score     (0-20): 1H setup alignment
  - Entry score     (0-20): 15M entry quality
  - Confidence score(0-20): Overall AI confidence calibration
  - Risk score      (0-20): Risk management quality (lower is riskier)

Total: 0-100 quality points.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.ai_scoring.mtf import MTFDecision
from app.strategies.features import FeatureSnapshot


@dataclass
class QualityReport:
    signal_id: Optional[int]
    symbol: str
    side: str
    total_score: float                # 0-100
    grade: str                        # A+ / A / B / C / D / F
    trend_score: float = 0.0
    structure_score: float = 0.0
    setup_score: float = 0.0
    entry_score: float = 0.0
    confidence_score: float = 0.0
    risk_score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @classmethod
    def grade_from_score(cls, score: float) -> str:
        if score >= 90:
            return "A+"
        if score >= 80:
            return "A"
        if score >= 70:
            return "B"
        if score >= 60:
            return "C"
        if score >= 50:
            return "D"
        return "F"


class SignalQualityScorer:
    """
    Evaluate signal quality from MTF decision and feature snapshots.
    Returns a QualityReport with per-component scores and reasoning.
    """

    def score(
        self,
        decision: MTFDecision,
        snaps: Dict[str, FeatureSnapshot],
        signal_id: Optional[int] = None,
        symbol: str = "",
    ) -> QualityReport:
        reasons: List[str] = []
        warnings: List[str] = []

        # ── Trend Score (0-20) ──────────────────────────────────────────
        trend_score = 0.0
        d1 = snaps.get("1d")
        if d1:
            bull = d1.ema_50 > d1.ema_200 and d1.last_close > d1.ema_200
            bear = d1.ema_50 < d1.ema_200 and d1.last_close < d1.ema_200
            if bull or bear:
                trend_score += 10.0
                reasons.append("Clear 1D trend direction")
                # EMA separation quality
                sep = abs(d1.ema_50 - d1.ema_200) / max(d1.ema_200, 1e-12)
                trend_score += min(5.0, sep * 500.0)
                if d1.structure.bos_bull or d1.structure.bos_bear:
                    trend_score += 5.0
                    reasons.append("1D structure confirmed")
            else:
                warnings.append("1D trend is unclear — choppy market")

        # ── Structure Score (0-20) ──────────────────────────────────────
        structure_score = 0.0
        h4 = snaps.get("4h")
        if h4:
            s = h4.structure
            hits = sum([
                (decision.side == "LONG" and s.bos_bull) or (decision.side == "SHORT" and s.bos_bear),
                (decision.side == "LONG" and s.mss_bull) or (decision.side == "SHORT" and s.mss_bear),
                (decision.side == "LONG" and s.ob_bull)  or (decision.side == "SHORT" and s.ob_bear),
                (decision.side == "LONG" and s.fvg_bull) or (decision.side == "SHORT" and s.fvg_bear),
                (decision.side == "LONG" and s.sweep_bull) or (decision.side == "SHORT" and s.sweep_bear),
            ])
            structure_score = min(20.0, hits * 4.0)
            if hits >= 3:
                reasons.append(f"Strong 4H structure ({hits}/5 confluences)")
            elif hits == 2:
                reasons.append("Adequate 4H structure (2/5 confluences)")
            else:
                warnings.append("Weak 4H structure")

        # ── Setup Score (0-20) ──────────────────────────────────────────
        setup_score = 0.0
        h1 = snaps.get("1h")
        if h1:
            s = h1.structure
            s_hits = sum([
                (decision.side == "LONG" and s.pullback_bull) or (decision.side == "SHORT" and s.pullback_bear),
                (decision.side == "LONG" and s.retest_bull) or (decision.side == "SHORT" and s.retest_bear),
                (decision.side == "LONG" and h1.above_vwap) or (decision.side == "SHORT" and not h1.above_vwap),
                (decision.side == "LONG" and h1.ema_fast > h1.ema_slow) or (decision.side == "SHORT" and h1.ema_fast < h1.ema_slow),
                h1.vol_spike_pct > 20.0,
            ])
            setup_score = min(20.0, s_hits * 4.0)
            if s_hits >= 4:
                reasons.append(f"High-quality 1H setup ({s_hits}/5)")
            elif s_hits >= 3:
                reasons.append(f"Good 1H setup ({s_hits}/5)")
            else:
                warnings.append(f"Marginal 1H setup ({s_hits}/5)")

        # ── Entry Score (0-20) ──────────────────────────────────────────
        entry_score = 0.0
        m15 = snaps.get("15m")
        if m15:
            s15 = m15.structure
            # BOS trigger
            bos_ok = (decision.side == "LONG" and s15.bos_bull) or (decision.side == "SHORT" and s15.bos_bear)
            # Alternative entry triggers
            fvg_ok = (decision.side == "LONG" and s15.fvg_bull) or (decision.side == "SHORT" and s15.fvg_bear)
            ob_ok = (decision.side == "LONG" and s15.ob_bull) or (decision.side == "SHORT" and s15.ob_bear)
            vwap_ok = (decision.side == "LONG" and m15.above_vwap) or (decision.side == "SHORT" and not m15.above_vwap)
            ema_ok = (
                (decision.side == "LONG" and m15.ema_fast > m15.ema_slow) or
                (decision.side == "SHORT" and m15.ema_fast < m15.ema_slow)
            )
            momentum_ok = (
                (decision.side == "LONG" and m15.momentum_bull) or
                (decision.side == "SHORT" and m15.momentum_bear)
            )
            vol_ok = m15.vol_spike_pct > 50.0
            macd_ok = (decision.side == "LONG" and m15.macd_hist > 0) or (decision.side == "SHORT" and m15.macd_hist < 0)

            entry_hits = sum([bos_ok, fvg_ok, ob_ok, vwap_ok, ema_ok])
            entry_score = min(12.0, entry_hits * 2.5)
            if momentum_ok:
                entry_score += 4.0
                reasons.append("15M momentum confirmed")
            if vol_ok:
                entry_score += 2.0
                reasons.append(f"15M volume spike (+{m15.vol_spike_pct:.0f}%)")
            if macd_ok:
                entry_score += 2.0
                reasons.append("15M MACD aligned")
            entry_score = min(20.0, entry_score)
            if bos_ok:
                reasons.append("15M BOS trigger")
            if fvg_ok:
                reasons.append("15M FVG entry")
            if ob_ok:
                reasons.append("15M Order Block")

        # ── Confidence Score (0-20) ─────────────────────────────────────
        # Map MTF confidence 75-100 → score 0-20
        confidence_score = max(0.0, min(20.0, (decision.confidence - 75.0) * 0.8))
        if decision.confidence >= 95:
            reasons.append("ELITE tier confidence")
        elif decision.confidence >= 85:
            reasons.append("VIP tier confidence")

        # ── Risk Score (0-20) — quality of risk/reward profile ──────────
        # Perfect score at RR >= 4, minimum at RR < 2
        risk_score = 0.0
        if m15:
            rr_score = min(12.0, max(0.0, (decision.fake_breakout_prob * -20.0 + 12.0)))
            fbp = decision.fake_breakout_prob
            if fbp < 0.2:
                risk_score += 8.0
                reasons.append("Low fake-breakout probability")
            elif fbp < 0.4:
                risk_score += 4.0
            else:
                warnings.append(f"Elevated fake-breakout risk ({fbp:.0%})")

            # ATR quality
            if m15.atr_pct <= 2.0:
                risk_score += 6.0
            elif m15.atr_pct <= 4.0:
                risk_score += 3.0
            else:
                warnings.append(f"High volatility (ATR {m15.atr_pct:.1f}%)")

            risk_score += rr_score
            risk_score = min(20.0, risk_score)

        total = round(
            trend_score + structure_score + setup_score +
            entry_score + confidence_score + risk_score,
            1
        )
        total = min(100.0, total)

        return QualityReport(
            signal_id=signal_id,
            symbol=symbol or (decision.primary_tf),
            side=decision.side,
            total_score=total,
            grade=QualityReport.grade_from_score(total),
            trend_score=round(trend_score, 1),
            structure_score=round(structure_score, 1),
            setup_score=round(setup_score, 1),
            entry_score=round(entry_score, 1),
            confidence_score=round(confidence_score, 1),
            risk_score=round(risk_score, 1),
            reasons=list(dict.fromkeys(reasons)),
            warnings=warnings,
        )
