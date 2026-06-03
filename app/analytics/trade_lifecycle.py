"""
Sprint 22C — Trade Lifecycle Analytics.

Measures the *quality* of a trade after entry, independent of whether it won:
how far price ran in our favour before reversing (MFE), how deep the drawdown
got before resolving (MAE), how long TP1 / SL took, and derived entry / SL / TP
quality scores. Aggregated across closed trades it answers questions like
"how much do we leave on the table before a stop?" and "are our stops too
tight for the regime?".

Pure functions: feed a closed-trade dict (+ optional intratrade price path) and
get a `TradeLifecycle`. Aggregation takes a list of those. No DB, no I/O. The
existing Signal model already stores `max_favorable_pct` / `max_adverse_pct`;
this module both *computes* them from a price path and *consumes* them when a
path is unavailable, so it degrades gracefully.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean
from typing import Optional, Sequence


@dataclass
class TradeLifecycle:
    symbol: str
    side: str
    mfe_percent: float = 0.0
    mae_percent: float = 0.0
    time_to_tp1_seconds: Optional[int] = None
    time_to_sl_seconds: Optional[int] = None
    time_to_tp2_seconds: Optional[int] = None
    entry_quality_score: float = 0.0  # 0-100
    sl_quality_score: float = 0.0
    tp_quality_score: float = 0.0
    recovery_after_drawdown: bool = False
    volatility_during_trade: float = 0.0
    outcome: str = ""  # TP1 | TP2 | TP3 | SL | OPEN | CLOSED

    def to_dict(self) -> dict:
        return asdict(self)


def _pct(frm: float, to: float) -> float:
    if not frm:
        return 0.0
    return (to - frm) / frm * 100.0


def _favorable(side: str, entry: float, price: float) -> float:
    """Signed favourable move % (positive = in our favour)."""
    p = _pct(entry, price)
    return p if (side or "").upper() == "LONG" else -p


def compute_lifecycle(
    trade: dict,
    price_path: Optional[Sequence[float]] = None,
) -> TradeLifecycle:
    """Build a TradeLifecycle from a closed/open trade dict.

    ``trade`` keys used (all optional, degrade gracefully):
      symbol, side/direction, entry (or entry_low/entry_high mid), stop_loss,
      tp1, tp2, status/outcome, max_favorable_pct, max_adverse_pct,
      time_to_tp1_seconds, time_to_sl_seconds.
    ``price_path`` is an optional sequence of post-entry prices (e.g. closes).
    """
    trade = trade or {}
    side = str(trade.get("side", trade.get("direction", "LONG"))).upper()
    symbol = str(trade.get("symbol", ""))

    entry = trade.get("entry")
    if entry is None:
        lo, hi = trade.get("entry_low"), trade.get("entry_high")
        if lo is not None and hi is not None:
            entry = (float(lo) + float(hi)) / 2
    entry = float(entry) if entry not in (None, 0) else 0.0

    sl = float(trade.get("stop_loss") or 0.0)
    tp1 = float(trade.get("tp1") or 0.0)

    # --- MFE / MAE ---
    mfe = float(trade.get("max_favorable_pct") or 0.0)
    mae = float(trade.get("max_adverse_pct") or 0.0)  # stored as a positive magnitude
    vol = 0.0
    if price_path and entry:
        favs = [_favorable(side, entry, p) for p in price_path if p]
        if favs:
            mfe = max(mfe, max(favs))
            mae = max(mae, max(0.0, -min(favs)))
            vol = round(max(favs) - min(favs), 4)

    outcome = str(trade.get("outcome", trade.get("status", ""))).upper()

    # --- Quality scores (0-100) ---
    # Entry quality: low adverse excursion relative to risk = good entry timing.
    risk_pct = abs(_pct(entry, sl)) if (entry and sl) else 0.0
    if risk_pct > 0:
        # 100 when MAE is 0; 0 when MAE >= risk (stop nearly hit).
        entry_q = max(0.0, min(100.0, 100.0 * (1.0 - mae / risk_pct)))
    else:
        entry_q = 0.0

    # SL quality: was the stop sized right? If we got stopped but MFE was large,
    # the stop was too tight (low score). If we won with small MAE, stop was fine.
    if outcome.startswith("SL"):
        sl_q = max(0.0, min(100.0, 100.0 * (1.0 - min(1.0, mfe / max(risk_pct, 1e-9)))))
    elif risk_pct > 0:
        sl_q = max(0.0, min(100.0, 100.0 * (1.0 - mae / (risk_pct * 1.5))))
    else:
        sl_q = 0.0

    # TP quality: how much of the favourable move did the target capture?
    reward_pct = abs(_pct(entry, tp1)) if (entry and tp1) else 0.0
    if mfe > 0 and reward_pct > 0:
        tp_q = max(0.0, min(100.0, 100.0 * (reward_pct / mfe)))
    else:
        tp_q = 0.0

    recovery = bool(mae > 0 and (outcome.startswith("TP") or mfe > mae))

    return TradeLifecycle(
        symbol=symbol,
        side=side,
        mfe_percent=round(mfe, 4),
        mae_percent=round(mae, 4),
        time_to_tp1_seconds=_opt_int(trade.get("time_to_tp1_seconds")),
        time_to_sl_seconds=_opt_int(trade.get("time_to_sl_seconds")),
        time_to_tp2_seconds=_opt_int(trade.get("time_to_tp2_seconds")),
        entry_quality_score=round(entry_q, 2),
        sl_quality_score=round(sl_q, 2),
        tp_quality_score=round(tp_q, 2),
        recovery_after_drawdown=recovery,
        volatility_during_trade=vol,
        outcome=outcome or "CLOSED",
    )


def _opt_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class LifecycleAggregate:
    sample_size: int = 0
    avg_mfe_before_sl: float = 0.0
    avg_mae_before_tp: float = 0.0
    avg_mfe: float = 0.0
    avg_mae: float = 0.0
    optimal_sl_distance_percent: Optional[float] = None
    optimal_tp_distance_percent: Optional[float] = None
    avg_entry_quality: float = 0.0
    avg_sl_quality: float = 0.0
    avg_tp_quality: float = 0.0
    avg_time_to_tp1_seconds: Optional[float] = None
    avg_time_to_sl_seconds: Optional[float] = None
    regime_performance: dict = None  # filled below

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["regime_performance"] is None:
            d["regime_performance"] = {}
        return d


def aggregate_lifecycles(
    lifecycles: Sequence[TradeLifecycle],
    regimes: Optional[Sequence[str]] = None,
) -> LifecycleAggregate:
    """Aggregate across many trades. ``regimes`` (parallel list) enables the
    regime-specific breakdown."""
    lcs = list(lifecycles)
    if not lcs:
        return LifecycleAggregate(regime_performance={})

    sl_trades = [c for c in lcs if c.outcome.startswith("SL")]
    tp_trades = [c for c in lcs if c.outcome.startswith("TP")]

    def _avg(xs):
        xs = [x for x in xs if x is not None]
        return round(mean(xs), 4) if xs else 0.0

    # Optimal SL distance ≈ worst MAE on winning trades + buffer (so winners
    # would not have been stopped). Optimal TP distance ≈ median MFE.
    optimal_sl = None
    if tp_trades:
        optimal_sl = round(max((c.mae_percent for c in tp_trades), default=0.0) * 1.1, 4)
    optimal_tp = None
    if lcs:
        mfes = sorted(c.mfe_percent for c in lcs)
        optimal_tp = round(mfes[len(mfes) // 2], 4)

    regime_perf: dict = {}
    if regimes and len(regimes) == len(lcs):
        for r, c in zip(regimes, lcs, strict=False):
            key = (r or "UNKNOWN").upper()
            bucket = regime_perf.setdefault(key, {"n": 0, "mfe": [], "mae": [], "tp_q": []})
            bucket["n"] += 1
            bucket["mfe"].append(c.mfe_percent)
            bucket["mae"].append(c.mae_percent)
            bucket["tp_q"].append(c.tp_quality_score)
        for key, b in regime_perf.items():
            regime_perf[key] = {
                "trades": b["n"],
                "avg_mfe": round(mean(b["mfe"]), 4) if b["mfe"] else 0.0,
                "avg_mae": round(mean(b["mae"]), 4) if b["mae"] else 0.0,
                "avg_tp_quality": round(mean(b["tp_q"]), 2) if b["tp_q"] else 0.0,
            }

    return LifecycleAggregate(
        sample_size=len(lcs),
        avg_mfe_before_sl=_avg([c.mfe_percent for c in sl_trades]),
        avg_mae_before_tp=_avg([c.mae_percent for c in tp_trades]),
        avg_mfe=_avg([c.mfe_percent for c in lcs]),
        avg_mae=_avg([c.mae_percent for c in lcs]),
        optimal_sl_distance_percent=optimal_sl,
        optimal_tp_distance_percent=optimal_tp,
        avg_entry_quality=_avg([c.entry_quality_score for c in lcs]),
        avg_sl_quality=_avg([c.sl_quality_score for c in lcs]),
        avg_tp_quality=_avg([c.tp_quality_score for c in lcs]),
        avg_time_to_tp1_seconds=_avg([c.time_to_tp1_seconds for c in lcs]) or None,
        avg_time_to_sl_seconds=_avg([c.time_to_sl_seconds for c in lcs]) or None,
        regime_performance=regime_perf,
    )
