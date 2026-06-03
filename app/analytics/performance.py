"""
Performance Analytics Engine.

Computes win rate, profit factor, Sharpe-like ratio, and per-symbol /
per-side breakdowns from the closed signal history.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import desc, select

from app.database.models import Signal
from app.database.session import SessionLocal


@dataclass
class SymbolStat:
    symbol: str
    total: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0
    avg_rr: float = 0.0


@dataclass
class SideStat:
    side: str
    total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0


@dataclass
class MonthStat:
    month: str       # YYYY-MM
    total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    best_pnl: float = 0.0
    worst_pnl: float = 0.0


@dataclass
class PerformanceReport:
    period_days: int
    total_signals: int = 0
    closed_signals: int = 0
    open_signals: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_rr: float = 0.0
    best_pnl: float = 0.0
    worst_pnl: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    long_stat: Optional[SideStat] = None
    short_stat: Optional[SideStat] = None
    by_symbol: List[SymbolStat] = field(default_factory=list)
    monthly: List[MonthStat] = field(default_factory=list)
    generated_at: str = ""


class PerformanceEngine:
    """Compute performance metrics from signal history."""

    async def compute(self, days: int = 30) -> PerformanceReport:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        report = PerformanceReport(
            period_days=days,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(Signal.created_at >= cutoff)
                .order_by(desc(Signal.created_at))
                .limit(5000)
            )
            signals: List[Signal] = res.scalars().all()

        if not signals:
            return report

        report.total_signals = len(signals)
        closed = [s for s in signals if s.status in ("TP1", "TP2", "TP3", "SL")]
        open_sigs = [s for s in signals if s.status == "OPEN"]
        wins = [s for s in closed if s.status in ("TP1", "TP2", "TP3")]
        losses = [s for s in closed if s.status == "SL"]

        report.closed_signals = len(closed)
        report.open_signals = len(open_sigs)
        report.wins = len(wins)
        report.losses = len(losses)
        report.win_rate = round(len(wins) / max(1, len(closed)) * 100, 1)

        pnls = [float(s.pnl_pct or 0) for s in closed]
        rrs = [float(s.risk_reward or 0) for s in closed]

        report.avg_pnl = round(sum(pnls) / max(1, len(pnls)), 2)
        report.avg_rr = round(sum(rrs) / max(1, len(rrs)), 2)
        report.best_pnl = round(max(pnls, default=0.0), 2)
        report.worst_pnl = round(min(pnls, default=0.0), 2)

        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        report.profit_factor = round(gross_win / max(0.001, gross_loss), 2)

        # Simple Sharpe-like ratio: avg_pnl / std_pnl
        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
            std = math.sqrt(variance)
            report.sharpe_ratio = round(mean / max(0.001, std), 2)

        # Max drawdown (peak-to-trough on cumulative PnL)
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in reversed(pnls):
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        report.max_drawdown_pct = round(max_dd, 2)

        # LONG / SHORT breakdown
        for side in ("LONG", "SHORT"):
            side_sigs = [s for s in closed if s.side == side]
            side_wins = [s for s in side_sigs if s.status in ("TP1", "TP2", "TP3")]
            side_pnls = [float(s.pnl_pct or 0) for s in side_sigs]
            stat = SideStat(
                side=side,
                total=len(side_sigs),
                wins=len(side_wins),
                losses=len(side_sigs) - len(side_wins),
                win_rate=round(len(side_wins) / max(1, len(side_sigs)) * 100, 1),
                avg_pnl=round(sum(side_pnls) / max(1, len(side_pnls)), 2),
            )
            if side == "LONG":
                report.long_stat = stat
            else:
                report.short_stat = stat

        # Per-symbol stats (top 20 by signal count)
        sym_map: Dict[str, List[Signal]] = {}
        for s in closed:
            sym_map.setdefault(s.symbol, []).append(s)
        sym_stats: List[SymbolStat] = []
        for sym, sigs in sym_map.items():
            sym_wins = [s for s in sigs if s.status in ("TP1", "TP2", "TP3")]
            sym_pnls = [float(s.pnl_pct or 0) for s in sigs]
            sym_rrs = [float(s.risk_reward or 0) for s in sigs]
            sym_stats.append(SymbolStat(
                symbol=sym,
                total=len(sigs),
                wins=len(sym_wins),
                losses=len(sigs) - len(sym_wins),
                total_pnl=round(sum(sym_pnls), 2),
                avg_pnl=round(sum(sym_pnls) / max(1, len(sym_pnls)), 2),
                win_rate=round(len(sym_wins) / max(1, len(sigs)) * 100, 1),
                avg_rr=round(sum(sym_rrs) / max(1, len(sym_rrs)), 2),
            ))
        report.by_symbol = sorted(sym_stats, key=lambda x: x.avg_pnl, reverse=True)[:20]

        # Monthly breakdown
        monthly_map: Dict[str, List[Signal]] = {}
        for s in closed:
            if s.created_at:
                key = s.created_at.strftime("%Y-%m")
                monthly_map.setdefault(key, []).append(s)
        monthly: List[MonthStat] = []
        for month, sigs in sorted(monthly_map.items()):
            m_wins = [s for s in sigs if s.status in ("TP1", "TP2", "TP3")]
            m_pnls = [float(s.pnl_pct or 0) for s in sigs]
            monthly.append(MonthStat(
                month=month,
                total=len(sigs),
                wins=len(m_wins),
                losses=len(sigs) - len(m_wins),
                win_rate=round(len(m_wins) / max(1, len(sigs)) * 100, 1),
                net_pnl=round(sum(m_pnls), 2),
                best_pnl=round(max(m_pnls, default=0.0), 2),
                worst_pnl=round(min(m_pnls, default=0.0), 2),
            ))
        report.monthly = monthly

        return report

    async def compute_as_dict(self, days: int = 30) -> dict:
        from dataclasses import asdict
        r = await self.compute(days)
        return asdict(r)
