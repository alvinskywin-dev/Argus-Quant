"""
Historical Backtest Engine.

Replays closed signals stored in the database and computes performance
metrics equivalent to what the live system would have reported.

Usage:
    engine = BacktestEngine()
    result = await engine.run(days=90)
    print(result.to_dict())
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import select

from app.database.models import Signal
from app.database.session import SessionLocal


@dataclass
class TradeRecord:
    signal_id: int
    symbol: str
    side: str
    timeframe: str
    confidence: float
    risk_reward: float
    entry: float
    tp1: float
    sl: float
    status: str
    pnl_pct: float
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]


@dataclass
class BacktestResult:
    period_days: int
    strategy: str = "MTF_SMC_STRICT"
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    open_trades: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_rr: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    net_pnl_pct: float = 0.0
    by_symbol: List[dict] = field(default_factory=list)
    by_timeframe: List[dict] = field(default_factory=list)
    by_side: List[dict] = field(default_factory=list)
    trades: List[TradeRecord] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict
        d = asdict(self)
        d["trades"] = [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in t.items()}
            for t in d["trades"]
        ]
        return d


class BacktestEngine:
    """
    Runs a backtest over historical signals from the database.

    Supports filtering by:
    - time period (days)
    - symbol (optional)
    - side (LONG/SHORT, optional)
    - timeframe (optional)
    - minimum confidence
    - minimum RR
    """

    async def run(
        self,
        days: int = 30,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        timeframe: Optional[str] = None,
        min_confidence: float = 0.0,
        min_rr: float = 0.0,
    ) -> BacktestResult:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = BacktestResult(
            period_days=days,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        async with SessionLocal() as session:
            q = select(Signal).where(
                Signal.created_at >= cutoff,
                Signal.status.in_(["TP1", "TP2", "TP3", "SL", "OPEN"]),
            )
            if symbol:
                q = q.where(Signal.symbol == symbol.upper())
            if side:
                q = q.where(Signal.side == side.upper())
            if timeframe:
                q = q.where(Signal.timeframe == timeframe)
            q = q.order_by(Signal.created_at)
            res = await session.execute(q)
            signals: List[Signal] = res.scalars().all()

        if not signals:
            return result

        # Apply additional numeric filters
        signals = [
            s for s in signals
            if float(s.confidence or 0) >= min_confidence
            and float(s.risk_reward or 0) >= min_rr
        ]

        trades = [TradeRecord(
            signal_id=s.id,
            symbol=s.symbol,
            side=s.side,
            timeframe=s.timeframe,
            confidence=round(float(s.confidence or 0), 1),
            risk_reward=round(float(s.risk_reward or 0), 2),
            entry=round(float(s.entry_low or 0), 6),
            tp1=round(float(s.tp1 or 0), 6),
            sl=round(float(s.stop_loss or 0), 6),
            status=s.status,
            pnl_pct=round(float(s.pnl_pct or 0), 2),
            opened_at=s.created_at,
            closed_at=s.closed_at,
        ) for s in signals]

        closed = [t for t in trades if t.status != "OPEN"]
        open_t = [t for t in trades if t.status == "OPEN"]
        wins = [t for t in closed if t.status in ("TP1", "TP2", "TP3")]
        losses = [t for t in closed if t.status == "SL"]

        result.total_trades = len(closed)
        result.wins = len(wins)
        result.losses = len(losses)
        result.open_trades = len(open_t)
        result.win_rate = round(len(wins) / max(1, len(closed)) * 100, 1)

        pnls = [t.pnl_pct for t in closed]
        rrs = [t.risk_reward for t in closed]

        result.avg_pnl = round(sum(pnls) / max(1, len(pnls)), 2)
        result.avg_rr = round(sum(rrs) / max(1, len(rrs)), 2)
        result.best_trade_pnl = round(max(pnls, default=0.0), 2)
        result.worst_trade_pnl = round(min(pnls, default=0.0), 2)
        result.net_pnl_pct = round(sum(pnls), 2)

        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        result.profit_factor = round(gross_win / max(0.001, gross_loss), 2)

        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls))
            result.sharpe_ratio = round(mean / max(0.001, std), 2)

        # Max drawdown
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown_pct = round(max_dd, 2)

        # By symbol
        sym_map: Dict[str, list] = {}
        for t in closed:
            sym_map.setdefault(t.symbol, []).append(t)
        result.by_symbol = sorted([{
            "symbol": sym,
            "total": len(ts),
            "wins": len([t for t in ts if t.status in ("TP1", "TP2", "TP3")]),
            "win_rate": round(len([t for t in ts if t.status in ("TP1", "TP2", "TP3")]) / max(1, len(ts)) * 100, 1),
            "avg_pnl": round(sum(t.pnl_pct for t in ts) / max(1, len(ts)), 2),
        } for sym, ts in sym_map.items()], key=lambda x: x["avg_pnl"], reverse=True)

        # By timeframe
        tf_map: Dict[str, list] = {}
        for t in closed:
            tf_map.setdefault(t.timeframe, []).append(t)
        result.by_timeframe = [{
            "timeframe": tf,
            "total": len(ts),
            "wins": len([t for t in ts if t.status in ("TP1", "TP2", "TP3")]),
            "win_rate": round(len([t for t in ts if t.status in ("TP1", "TP2", "TP3")]) / max(1, len(ts)) * 100, 1),
        } for tf, ts in tf_map.items()]

        # By side
        for sd in ("LONG", "SHORT"):
            sd_ts = [t for t in closed if t.side == sd]
            sd_wins = [t for t in sd_ts if t.status in ("TP1", "TP2", "TP3")]
            sd_pnls = [t.pnl_pct for t in sd_ts]
            result.by_side.append({
                "side": sd,
                "total": len(sd_ts),
                "wins": len(sd_wins),
                "win_rate": round(len(sd_wins) / max(1, len(sd_ts)) * 100, 1),
                "avg_pnl": round(sum(sd_pnls) / max(1, len(sd_pnls)), 2),
            })

        result.trades = trades
        return result
