"""
Strict Multi-Timeframe (MTF) signal pipeline.

Workflow:
    1D  →  Trend Filter      (EMA50/200, market structure)
    4H  →  Structure          (BOS, CHoCH, OB, FVG, Sweep)
    1H  →  Setup Detection    (Pullback, Retest, VWAP, EMA align, Volume)
    15M →  Entry Trigger V2   (5 factors × 1 point each, configurable threshold)

Each layer is a hard gate — if it fails the pipeline stops and reports why.

Entry Engine V2:
    Factors (each worth 1 point, max score = 5):
        1. BOS          — break of structure on 15M
        2. FVG retest   — fair value gap in play on 15M
        3. OB retest    — price inside order block on 15M
        4. EMA pullback — 15M EMA9/21 aligned + close above/below slow EMA
        5. VWAP reclaim — 15M price on correct side of VWAP
    Threshold: ENTRY_PASS_SCORE (default 2, set via env ENTRY_PASS_SCORE)

Confidence scoring (0-100):
    Base 75 for passing all four layers at minimum conditions.
    Up to +25 bonus points for stronger signals → 95-100 = ELITE.

Tiers:
    95-100  ELITE
    85-94   VIP
    75-84   PUBLIC
    <75     Reject
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.config import settings
from app.strategies.features import FeatureSnapshot

# The four required timeframes in pipeline order (trend → entry)
PIPELINE_TFS: Tuple[str, ...] = ("1d", "4h", "1h", "15m")

TIER_ELITE = 95.0
TIER_VIP = 85.0
TIER_PUBLIC = 75.0


@dataclass
class MTFDecision:
    side: str
    confidence: float
    tier: str
    risk_level: str
    primary_tf: str
    contributing_tfs: List[str]
    reasons: List[str]
    fake_breakout_prob: float
    # Per-layer scores (0-based raw counts/points for the detail page)
    trend_score: float = 0.0
    structure_score: float = 0.0
    setup_score: float = 0.0
    entry_score_pts: float = 0.0
    # Entry factor breakdown for diagnostics
    entry_factors: Dict[str, bool] = field(default_factory=dict)


@dataclass
class MTFRejection:
    """Carries the reason a signal was rejected and at which stage."""
    side: str
    stage: str       # trend | structure | setup | entry | confidence | rr | cooldown
    detail: str
    # Partial scores computed before rejection (zero if stage not yet reached)
    trend_score: float = 0.0
    structure_score: float = 0.0
    setup_score: float = 0.0
    entry_score_pts: float = 0.0
    entry_factors: Dict[str, bool] = field(default_factory=dict)


# ─── helpers ────────────────────────────────────────────────────────────────

def _risk_classify(snap: FeatureSnapshot) -> str:
    if snap.atr_pct >= 4.0 or snap.bb_width >= 0.18:
        return "HIGH"
    if snap.atr_pct <= 1.0 and snap.bb_width <= 0.05:
        return "LOW"
    return "MEDIUM"


def _fake_breakout_prob(snap: FeatureSnapshot, side: str) -> float:
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


def _tier(confidence: float) -> str:
    if confidence >= TIER_ELITE:
        return "ELITE"
    if confidence >= TIER_VIP:
        return "VIP"
    if confidence >= TIER_PUBLIC:
        return "PUBLIC"
    return "NONE"


# ─── main pipeline ──────────────────────────────────────────────────────────

def evaluate_pipeline(
    snaps: Dict[str, FeatureSnapshot],
) -> Tuple[Optional[MTFDecision], Optional[MTFRejection]]:
    """
    Run the strict 4-layer MTF pipeline.
    Returns (MTFDecision, None) on success or (None, MTFRejection) on failure.
    """
    # Require all four timeframes
    missing = [tf for tf in PIPELINE_TFS if tf not in snaps]
    if missing:
        return None, MTFRejection(
            side="?", stage="no_data",
            detail=f"missing timeframes: {', '.join(missing)}",
        )

    d1  = snaps["1d"]
    h4  = snaps["4h"]
    h1  = snaps["1h"]
    m15 = snaps["15m"]

    # ── Layer 1: 1D Trend Filter ────────────────────────────────────────
    d1_bull = d1.ema_50 > d1.ema_200 and d1.last_close > d1.ema_200
    d1_bear = d1.ema_50 < d1.ema_200 and d1.last_close < d1.ema_200

    if d1_bull:
        side = "LONG"
    elif d1_bear:
        side = "SHORT"
    else:
        return None, MTFRejection(
            side="?", stage="trend",
            detail=(
                f"No clear 1D trend — "
                f"EMA50={d1.ema_50:.4f} EMA200={d1.ema_200:.4f} "
                f"close={d1.last_close:.4f}"
            ),
            trend_score=0.0,
        )

    d1s = d1.structure
    d1_struct_ok = (
        (side == "LONG"  and (d1s.bos_bull or d1s.mss_bull)) or
        (side == "SHORT" and (d1s.bos_bear or d1s.mss_bear))
    )

    # Trend score: 10 base + up to 5 for separation + 5 for structure = max 20
    ema_sep = abs(d1.ema_50 - d1.ema_200) / max(d1.ema_200, 1e-12)
    trend_score = 10.0 + min(5.0, ema_sep * 500.0) + (5.0 if d1_struct_ok else 0.0)

    trend_reasons: List[str] = [
        f"1D trend {side}: EMA50 {'>' if side == 'LONG' else '<'} EMA200"
    ]
    if d1_struct_ok:
        trend_reasons.append("1D market structure confirmed")

    # ── Layer 2: 4H Structure Confirmation ─────────────────────────────
    s4 = h4.structure
    struct_map = {
        "BOS":    (side == "LONG" and s4.bos_bull)   or (side == "SHORT" and s4.bos_bear),
        "CHoCH":  (side == "LONG" and s4.mss_bull)   or (side == "SHORT" and s4.mss_bear),
        "OB":     (side == "LONG" and s4.ob_bull)    or (side == "SHORT" and s4.ob_bear),
        "FVG":    (side == "LONG" and s4.fvg_bull)   or (side == "SHORT" and s4.fvg_bear),
        "Sweep":  (side == "LONG" and s4.sweep_bull) or (side == "SHORT" and s4.sweep_bear),
    }
    struct_hits = sum(struct_map.values())

    if struct_hits < 2:
        hit_list = ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in struct_map.items())
        return None, MTFRejection(
            side=side, stage="structure",
            detail=f"4H structure weak ({struct_hits}/5): {hit_list}",
            trend_score=trend_score,
        )

    # Structure bonus: 0 for hitting minimum, +2 per extra signal, max +6
    struct_bonus = min(6.0, max(0.0, (struct_hits - 2) * 2.0))

    struct_reasons = [f"4H {k}" for k, v in struct_map.items() if v]

    # ── Layer 3: 1H Setup Detection ────────────────────────────────────
    s1 = h1.structure
    setup_map = {
        "Pullback": (side == "LONG" and s1.pullback_bull) or (side == "SHORT" and s1.pullback_bear),
        "Retest":   (side == "LONG" and s1.retest_bull)   or (side == "SHORT" and s1.retest_bear),
        "VWAP":     (side == "LONG" and h1.above_vwap)    or (side == "SHORT" and not h1.above_vwap),
        "EMA_align":(
            (side == "LONG"  and h1.ema_fast > h1.ema_slow and h1.last_close > h1.ema_slow) or
            (side == "SHORT" and h1.ema_fast < h1.ema_slow and h1.last_close < h1.ema_slow)
        ),
        "Volume":   h1.vol_spike_pct > 20.0,
    }
    setup_hits = sum(setup_map.values())

    if setup_hits < 3:
        hit_list = ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in setup_map.items())
        return None, MTFRejection(
            side=side, stage="setup",
            detail=f"1H setup weak ({setup_hits}/5): {hit_list}",
            trend_score=trend_score,
            structure_score=float(struct_hits),
        )

    # Setup bonus: 0 at 3 signals, +2 each extra, max +4
    setup_bonus = min(4.0, max(0.0, (setup_hits - 3) * 2.0))

    setup_reasons = [f"1H {k.replace('_', ' ')}" for k, v in setup_map.items() if v]

    # ── Layer 4: 15M Entry Trigger V2 (5 factors × 1 point each) ──────
    #
    # Five equal-weight factors — any combination of ≥ ENTRY_PASS_SCORE passes.
    # Score 0-5; threshold configurable via env ENTRY_PASS_SCORE (default 2).
    #
    #   1. BOS          — price broke the 20-bar swing high/low on 15M
    #   2. FVG retest   — a 15M fair value gap is open and price is inside it
    #   3. OB retest    — price is retesting a 15M order block
    #   4. EMA pullback — 15M EMA9 > EMA21 and close on correct side of EMA21
    #   5. VWAP reclaim — price is on the entry-direction side of 15M VWAP

    ENTRY_MIN_SCORE: int = settings.entry_pass_score  # default 2

    s15 = m15.structure
    entry_factors: Dict[str, bool] = {
        "BOS":         (side == "LONG" and s15.bos_bull)   or (side == "SHORT" and s15.bos_bear),
        "FVG retest":  (side == "LONG" and s15.fvg_bull)   or (side == "SHORT" and s15.fvg_bear),
        "OB retest":   (side == "LONG" and s15.ob_bull)    or (side == "SHORT" and s15.ob_bear),
        "EMA pullback":(
            (side == "LONG"  and m15.ema_fast > m15.ema_slow and m15.last_close > m15.ema_slow) or
            (side == "SHORT" and m15.ema_fast < m15.ema_slow and m15.last_close < m15.ema_slow)
        ),
        "VWAP reclaim":(side == "LONG" and m15.above_vwap) or (side == "SHORT" and not m15.above_vwap),
    }

    entry_score = sum(1 for v in entry_factors.values() if v)
    entry_reasons: List[str] = [f"15M {k}" for k, v in entry_factors.items() if v]

    if entry_score < ENTRY_MIN_SCORE:
        hit_list = ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in entry_factors.items())
        return None, MTFRejection(
            side=side, stage="entry",
            detail=(
                f"15M entry score {entry_score}/{ENTRY_MIN_SCORE} — {hit_list}"
            ),
            trend_score=trend_score,
            structure_score=float(struct_hits),
            setup_score=float(setup_hits),
            entry_score_pts=float(entry_score),
            entry_factors=entry_factors,
        )

    # Map entry score (0-5) → confidence bonus (0-10): each factor adds 2 pts
    entry_bonus = min(10.0, float(entry_score) * 2.0)

    # ── Final confidence ────────────────────────────────────────────────
    # Base 75 for passing all 4 layers + bonuses (max 25) = 75-100
    confidence = round(
        min(100.0, 75.0 + trend_score - 10.0 + struct_bonus + setup_bonus + entry_bonus),
        1,
    )
    # trend_score carries its own base; normalise: subtract 10 (the guaranteed minimum)
    # so bonus from trend is 0..10 (0-5 EMA sep + 0-5 struct), added on top of 75.

    all_reasons = list(dict.fromkeys(
        trend_reasons + struct_reasons + setup_reasons + entry_reasons
    ))

    return MTFDecision(
        side=side,
        confidence=confidence,
        tier=_tier(confidence),
        risk_level=_risk_classify(m15),
        primary_tf="15m",
        contributing_tfs=list(PIPELINE_TFS),
        reasons=all_reasons,
        fake_breakout_prob=_fake_breakout_prob(m15, side),
        trend_score=round(trend_score, 1),
        structure_score=round(float(struct_hits), 1),
        setup_score=round(float(setup_hits), 1),
        entry_score_pts=round(float(entry_score), 1),
        entry_factors=entry_factors,
    ), None


# ─── legacy shim (keeps existing callers working) ──────────────────────────

def aggregate(
    scores_by_tf: Dict,
    primary_tf: str,
    snaps: Optional[Dict[str, FeatureSnapshot]] = None,
) -> Optional[MTFDecision]:
    """
    Legacy entry point kept for backward compatibility.
    If `snaps` are provided it delegates to the new strict pipeline.
    Otherwise falls back to the old weighted-average aggregation.
    """
    if snaps is not None:
        decision, _ = evaluate_pipeline(snaps)
        return decision

    # ── old weighted-average path (fallback only) ───────────────────────
    if not scores_by_tf or primary_tf not in scores_by_tf:
        return None

    TF_WEIGHTS: Dict[str, float] = {
        "1m": 0.05, "5m": 0.15, "15m": 0.20, "30m": 0.22,
        "1h": 0.25, "2h": 0.27, "4h": 0.30,  "6h": 0.32,
        "12h": 0.33, "1d": 0.35,
    }

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
    mtf_bonus = min(0.25, 0.08 * max(0, len(aligned_tfs) - 1))
    final_conf = min(100.0, avg_conf * (1.0 + mtf_bonus))

    if conflict_weight > 0.2:
        final_conf *= 1.0 - min(0.4, conflict_weight)
        reasons.append("Higher-TF conflict — confidence reduced")

    if len(aligned_tfs) >= 2:
        reasons.append(f"MTF confluence: {', '.join(aligned_tfs)}")

    return MTFDecision(
        side=side,
        confidence=round(final_conf, 1),
        tier=_tier(final_conf),
        risk_level=primary.risk_level,
        primary_tf=primary_tf,
        contributing_tfs=aligned_tfs,
        reasons=list(dict.fromkeys(reasons)),
        fake_breakout_prob=primary.fake_breakout_prob,
    )
