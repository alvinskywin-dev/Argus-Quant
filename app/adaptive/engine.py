"""
Sprint 18 — Adaptive Threshold Engine.

Analyzes the last N closed signals and automatically adjusts:
  - MIN_CONFIDENCE (adaptive_min_confidence system setting)
  - ENTRY_PASS_SCORE (adaptive_entry_pass_score system setting)
  - MIN_RR (adaptive_min_rr system setting)

Safety limits (never exceeded):
  MIN_CONFIDENCE : 70-95
  ENTRY_PASS_SCORE: 1-5
  MIN_RR          : 1.5-4.0

The engine writes adapted values to the system_settings table.
The scanner reads those values at runtime (if adaptive_thresholds=true).
Adaptation step is ±2.5 for confidence, ±0.5 for RR, ±1 for entry_pass.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from app.config import settings
from app.database.models import Signal
from app.database.repo import get_setting, set_setting
from app.database.session import SessionLocal

logger = logging.getLogger(__name__)

WIN_STATUSES = {"TP1", "TP2", "TP3"}
CLOSED_STATUSES = ["TP1", "TP2", "TP3", "SL"]

# Safety clamps
CONF_MIN, CONF_MAX = 70.0, 95.0
ENTRY_MIN, ENTRY_MAX = 1, 5
RR_MIN, RR_MAX = 1.5, 4.0

# Confidence buckets used for analysis
_BUCKETS: List[Tuple[float, float]] = [
    (70, 75), (75, 80), (80, 85), (85, 90), (90, 95),
]


def _win_rate(sigs: List) -> Optional[float]:
    if not sigs:
        return None
    return sum(1 for s in sigs if s.status in WIN_STATUSES) / len(sigs) * 100.0


async def run_adaptive_cycle() -> Dict[str, Any]:
    """
    Run one adaptive-threshold cycle.  Returns a summary dict of what changed.
    No-ops if adaptive_thresholds is disabled or sample is too small.
    """
    if not settings.adaptive_thresholds:
        return {"skipped": True, "reason": "adaptive_thresholds=false"}

    # ── Load closed signals ───────────────────────────────────────────────
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Signal)
            .where(Signal.status.in_(CLOSED_STATUSES))
            .order_by(desc(Signal.closed_at))
            .limit(settings.adaptive_lookback)
        )
        closed: List[Signal] = list(rows.scalars().all())

    if len(closed) < settings.adaptive_min_trades:
        return {
            "skipped": True,
            "reason": f"only {len(closed)} trades, need {settings.adaptive_min_trades}",
        }

    # ── Build confidence bucket stats ─────────────────────────────────────
    bucket_sigs: Dict[Tuple[float, float], List] = {b: [] for b in _BUCKETS}
    for sig in closed:
        conf = float(sig.confidence or 0)
        for lo, hi in _BUCKETS:
            if lo <= conf < hi:
                bucket_sigs[(lo, hi)].append(sig)
                break

    bucket_wr: Dict[Tuple[float, float], float] = {}
    for bkt, sigs in bucket_sigs.items():
        wr = _win_rate(sigs)
        if wr is not None:
            bucket_wr[bkt] = wr

    # ── Read current adaptive thresholds (or fall back to config) ─────────
    cur_conf = float(await get_setting("adaptive_min_confidence", str(settings.min_confidence)))
    cur_entry = int(await get_setting("adaptive_entry_pass_score", str(settings.entry_pass_score)))
    cur_rr = float(await get_setting("adaptive_min_rr", str(settings.min_rr)))

    changes: Dict[str, Any] = {}

    # ── MIN_CONFIDENCE adaptation ─────────────────────────────────────────
    cur_bucket = next(
        (b for b in _BUCKETS if b[0] <= cur_conf < b[1]), None
    )

    if cur_bucket and cur_bucket in bucket_wr and len(bucket_sigs[cur_bucket]) >= 5:
        cur_wr = bucket_wr[cur_bucket]

        # Check next higher bucket — if it wins ≥5% more, raise threshold
        next_bkt = (cur_bucket[1], cur_bucket[1] + 5)
        if (
            next_bkt in bucket_wr
            and len(bucket_sigs.get(next_bkt, [])) >= 5
            and bucket_wr[next_bkt] >= cur_wr + 5.0
        ):
            new_conf = min(CONF_MAX, cur_conf + 2.5)
            if new_conf != cur_conf:
                await set_setting("adaptive_min_confidence", str(new_conf))
                changes["min_confidence"] = {"from": cur_conf, "to": new_conf, "reason": "higher bucket outperforms"}

        # If current bucket underperforms (<40% WR) and lower bucket is better, lower threshold
        elif cur_wr < 40.0 and not changes.get("min_confidence"):
            prev_bkt = (cur_bucket[0] - 5, cur_bucket[0])
            if (
                prev_bkt in bucket_wr
                and len(bucket_sigs.get(prev_bkt, [])) >= 5
                and bucket_wr[prev_bkt] >= cur_wr + 5.0
                and prev_bkt[0] >= CONF_MIN
            ):
                new_conf = max(CONF_MIN, cur_conf - 2.5)
                if new_conf != cur_conf:
                    await set_setting("adaptive_min_confidence", str(new_conf))
                    changes["min_confidence"] = {"from": cur_conf, "to": new_conf, "reason": "current bucket underperforms"}

    # ── MIN_RR adaptation ─────────────────────────────────────────────────
    # Compare win rate for signals with rr >= cur_rr + 0.5 vs all closed
    overall_wr = _win_rate(closed) or 50.0
    high_rr = [s for s in closed if float(s.risk_reward or 0) >= cur_rr + 0.5]
    high_rr_wr = _win_rate(high_rr)

    if high_rr_wr is not None and len(high_rr) >= 5 and high_rr_wr >= overall_wr + 5.0:
        new_rr = min(RR_MAX, cur_rr + 0.5)
        if new_rr != cur_rr:
            await set_setting("adaptive_min_rr", str(new_rr))
            changes["min_rr"] = {"from": cur_rr, "to": new_rr, "reason": "higher RR outperforms"}
    elif overall_wr < 40.0:
        low_rr = [s for s in closed if float(s.risk_reward or 0) >= max(RR_MIN, cur_rr - 0.5)]
        low_rr_wr = _win_rate(low_rr)
        if low_rr_wr is not None and len(low_rr) >= 5:
            new_rr = max(RR_MIN, cur_rr - 0.5)
            if new_rr != cur_rr:
                await set_setting("adaptive_min_rr", str(new_rr))
                changes["min_rr"] = {"from": cur_rr, "to": new_rr, "reason": "overall WR poor, relaxing RR"}

    summary = {
        "sample_size": len(closed),
        "overall_winrate": round(overall_wr, 1),
        "current_thresholds": {"min_confidence": cur_conf, "entry_pass_score": cur_entry, "min_rr": cur_rr},
        "changes": changes,
        "bucket_winrates": {
            f"{int(lo)}-{int(hi)}": round(wr, 1)
            for (lo, hi), wr in bucket_wr.items()
        },
    }
    if changes:
        logger.info(f"[Adaptive] thresholds updated: {changes}")
    else:
        logger.debug("[Adaptive] cycle ran, no threshold changes needed")

    return summary
