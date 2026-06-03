"""
Sprint 16B — Auto Winrate Analyzer.

Computes rolling win-rate statistics across multiple dimensions
(side, confidence bucket, RR bucket, timeframe, funding class, OI trend).
Called on-demand by the dashboard API — no background task needed.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from app.database.models import Signal
from app.database.session import SessionLocal

WIN_STATUSES = {"TP1", "TP2", "TP3"}
CLOSED_STATUSES = ["TP1", "TP2", "TP3", "SL"]

_CONF_BUCKETS = [
    ("70-75", 70.0, 75.0),
    ("75-80", 75.0, 80.0),
    ("80-85", 80.0, 85.0),
    ("85-90", 85.0, 90.0),
    ("90+",   90.0, 101.0),
]

_RR_BUCKETS = [
    ("1.5-2.0", 1.5, 2.0),
    ("2.0-2.5", 2.0, 2.5),
    ("2.5-3.0", 2.5, 3.0),
    ("3.0+",    3.0, 99.0),
]

_TF_BUCKETS = ["15m", "1h", "4h", "1d"]

# Stop-Loss Engine V2 — winrate by SL distance and SL method.
_SL_DIST_BUCKETS = [
    ("0-2%",  0.0,  2.0),
    ("2-4%",  2.0,  4.0),
    ("4-6%",  4.0,  6.0),
    ("6-10%", 6.0,  10.0),
    ("10%+",  10.0, 1e9),
]
# Methods we surface explicitly (1D support stop / ATR stop / structure stop).
_SL_METHODS = ["PREV_1D_SUPPORT", "atr", "structure", "liquidity"]


def _wr(sigs: List) -> Optional[float]:
    if not sigs:
        return None
    wins = sum(1 for s in sigs if s.status in WIN_STATUSES)
    return round(wins / len(sigs) * 100.0, 1)


def _bucket_stats(sigs: List, label: str) -> Dict[str, Any]:
    wr = _wr(sigs)
    return {"label": label, "winrate": wr, "count": len(sigs)}


async def compute_winrate_analysis(limit: int = 500) -> Dict[str, Any]:
    """
    Analyze the last *limit* closed signals across multiple dimensions.
    Returns a dict suitable for JSON serialisation.
    """
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Signal)
            .where(Signal.status.in_(CLOSED_STATUSES))
            .order_by(desc(Signal.closed_at))
            .limit(limit)
        )
        closed: List[Signal] = list(rows.scalars().all())

    if not closed:
        return {
            "sample_size": 0,
            "long_winrate": None,
            "short_winrate": None,
            "best_confidence_bucket": None,
            "best_timeframe": None,
            "best_rr_bucket": None,
        }

    # ── Side win rates ────────────────────────────────────────────────────
    longs  = [s for s in closed if s.side == "LONG"]
    shorts = [s for s in closed if s.side == "SHORT"]

    # ── Confidence buckets ────────────────────────────────────────────────
    conf_stats: List[Dict] = []
    for label, lo, hi in _CONF_BUCKETS:
        bucket = [s for s in closed if lo <= float(s.confidence or 0) < hi]
        conf_stats.append(_bucket_stats(bucket, label))

    # ── RR buckets ────────────────────────────────────────────────────────
    rr_stats: List[Dict] = []
    for label, lo, hi in _RR_BUCKETS:
        bucket = [s for s in closed if lo <= float(s.risk_reward or 0) < hi]
        rr_stats.append(_bucket_stats(bucket, label))

    # ── Timeframe buckets ─────────────────────────────────────────────────
    tf_stats: List[Dict] = []
    for tf in _TF_BUCKETS:
        bucket = [s for s in closed if s.timeframe == tf]
        tf_stats.append(_bucket_stats(bucket, tf))

    # ── Diagnostics-based breakdowns (funding / OI) ───────────────────────
    funding_pos_sigs: List = []
    funding_neg_sigs: List = []
    oi_rising_sigs:   List = []
    oi_falling_sigs:  List = []

    # Stop-Loss Engine V2 — group closed signals by SL method and SL distance.
    sl_method_groups: Dict[str, List] = {m: [] for m in _SL_METHODS}
    sl_dist_groups: Dict[str, List] = {label: [] for label, _, _ in _SL_DIST_BUCKETS}

    for s in closed:
        if not s.diagnostics:
            continue
        try:
            diag = json.loads(s.diagnostics)
        except Exception:
            continue
        fclass = diag.get("funding_class")
        if fclass in ("positive", "extreme_positive"):
            funding_pos_sigs.append(s)
        elif fclass in ("negative", "extreme_negative"):
            funding_neg_sigs.append(s)
        oi_sc = diag.get("oi_score", 0)
        if oi_sc > 0:
            oi_rising_sigs.append(s)
        elif oi_sc < 0:
            oi_falling_sigs.append(s)

        # SL method (falls back to the legacy rr_method-style label when absent)
        sl_method = diag.get("stoploss_method")
        if sl_method in sl_method_groups:
            sl_method_groups[sl_method].append(s)

        # SL distance bucket — prefer the stored diagnostic, else derive from levels
        sl_dist = diag.get("sl_distance_percent")
        if sl_dist is None and s.stop_loss and s.entry_low and s.entry_high:
            mid = (s.entry_low + s.entry_high) / 2.0
            if mid:
                sl_dist = abs(mid - s.stop_loss) / mid * 100.0
        if sl_dist is not None:
            for label, lo, hi in _SL_DIST_BUCKETS:
                if lo <= float(sl_dist) < hi:
                    sl_dist_groups[label].append(s)
                    break

    # ── Best in each category ─────────────────────────────────────────────
    def _best(stats: List[Dict]) -> Optional[str]:
        valid = [b for b in stats if b["winrate"] is not None and b["count"] >= 3]
        return max(valid, key=lambda b: b["winrate"])["label"] if valid else None

    return {
        "sample_size":            len(closed),
        "long_winrate":           _wr(longs),
        "short_winrate":          _wr(shorts),
        "funding_positive_winrate": _wr(funding_pos_sigs),
        "funding_negative_winrate": _wr(funding_neg_sigs),
        "oi_rising_winrate":      _wr(oi_rising_sigs),
        "oi_falling_winrate":     _wr(oi_falling_sigs),
        "best_confidence_bucket": _best(conf_stats),
        "best_rr_bucket":         _best(rr_stats),
        "best_timeframe":         _best(tf_stats),
        "confidence_buckets":     conf_stats,
        "rr_buckets":             rr_stats,
        "timeframe_buckets":      tf_stats,
        # Stop-Loss Engine V2 analytics
        "sl_method_buckets":      [_bucket_stats(sl_method_groups[m], m) for m in _SL_METHODS],
        "sl_distance_buckets":    [
            _bucket_stats(sl_dist_groups[label], label) for label, _, _ in _SL_DIST_BUCKETS
        ],
        "best_sl_method":         _best([
            _bucket_stats(sl_method_groups[m], m) for m in _SL_METHODS
        ]),
    }
