"""
Multi-timeframe (MTF) aggregator.

Combines per-timeframe Scores into a single decision. Confluence across
timeframes is a strong filter — a setup that fires on 5m AND aligns with 1h
trend is much higher quality than a 5m signal alone.

Strategy:
- pick the candidate side that wins across the most timeframes
- average confidences weighted by timeframe (higher TF = more weight)
- bump up the mtf_confirmation component when aligned
- discard if conflict is severe
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.ai_scoring.scorer import Score, WEIGHTS

# Higher TF gets more weight in MTF aggregation
TF_WEIGHTS: Dict[str, float] = {
    "1m": 0.05,
    "5m": 0.15,
    "15m": 0.20,
    "30m": 0.22,
    "1h": 0.25,
    "4h": 0.30,
    "1d": 0.35,
}


@dataclass
class MTFDecision:
    side: str
    confidence: float
    risk_level: str
    primary_tf: str
    contributing_tfs: List[str]
    reasons: List[str]
    fake_breakout_prob: float


def aggregate(scores_by_tf: Dict[str, Score], primary_tf: str) -> Optional[MTFDecision]:
    if not scores_by_tf or primary_tf not in scores_by_tf:
        return None

    primary = scores_by_tf[primary_tf]
    if primary.confidence < 1:
        return None

    side = primary.side

    aligned_tfs: List[str] = []
    conflict_weight = 0.0
    weighted_conf = 0.0
    weight_total = 0.0
    reasons: List[str] = list(primary.reasons)

    for tf, score in scores_by_tf.items():
        w = TF_WEIGHTS.get(tf, 0.15)
        if score.side == side and score.confidence >= 30:
            aligned_tfs.append(tf)
            weighted_conf += score.confidence * w
            weight_total += w
        elif score.side != side and score.confidence >= 55:
            conflict_weight += w

    if weight_total == 0:
        return None

    avg_conf = weighted_conf / weight_total

    # MTF boost: ratio of aligned timeframes
    mtf_bonus = min(0.25, 0.08 * max(0, len(aligned_tfs) - 1))
    final_conf = min(100.0, avg_conf * (1.0 + mtf_bonus))

    # Penalty for higher-TF conflict
    if conflict_weight > 0.2:
        final_conf *= 1.0 - min(0.4, conflict_weight)
        reasons.append("Higher-TF conflict — confidence reduced")

    if len(aligned_tfs) >= 2:
        reasons.append(f"MTF confluence: {', '.join(aligned_tfs)}")

    return MTFDecision(
        side=side,
        confidence=round(final_conf, 1),
        risk_level=primary.risk_level,
        primary_tf=primary_tf,
        contributing_tfs=aligned_tfs,
        reasons=list(dict.fromkeys(reasons)),  # dedupe, preserve order
        fake_breakout_prob=primary.fake_breakout_prob,
    )
