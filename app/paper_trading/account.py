"""
Paper Trading Account.

Simulates trade execution with a virtual balance.
No real money is involved — this is for testing only.

Enabled via PAPER_TRADING=true in .env.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.config import settings
from app.utils.logger import logger


@dataclass
class PaperPosition:
    id: int
    symbol: str
    side: str          # LONG / SHORT
    entry_price: float
    size_usdt: float
    tp1: float
    tp2: float
    tp3: float
    stop_loss: float
    risk_reward: float
    confidence: float
    status: str = "OPEN"  # OPEN / TP1 / TP2 / TP3 / SL / CANCELLED
    pnl_usdt: float = 0.0
    pnl_pct: float = 0.0
    opened_at: str = ""
    closed_at: str = ""

    def __post_init__(self):
        if not self.opened_at:
            self.opened_at = datetime.now(timezone.utc).isoformat()


@dataclass
class PaperAccountState:
    balance_usdt: float
    equity_usdt: float
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl_usdt: float = 0.0
    total_pnl_pct: float = 0.0
    open_positions: List[PaperPosition] = field(default_factory=list)
    closed_positions: List[PaperPosition] = field(default_factory=list)


class PaperAccount:
    """
    Thread-safe paper trading account.

    Manages virtual balance, position sizing, TP/SL simulation.
    """

    def __init__(self) -> None:
        self._balance = settings.paper_initial_balance
        self._initial_balance = settings.paper_initial_balance
        self._positions: Dict[int, PaperPosition] = {}
        self._history: List[PaperPosition] = []
        self._next_id = 1
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return settings.paper_trading

    async def open_position(self, signal: dict) -> Optional[PaperPosition]:
        """Open a paper position from a signal dict."""
        if not self.enabled:
            return None

        risk_pct = settings.paper_risk_per_trade_pct / 100.0
        size_usdt = round(self._balance * risk_pct, 2)

        if size_usdt <= 0:
            logger.warning("paper: insufficient balance to open position")
            return None

        async with self._lock:
            pos = PaperPosition(
                id=self._next_id,
                symbol=signal["symbol"],
                side=signal["side"],
                entry_price=float(signal.get("entry_low", 0)),
                size_usdt=size_usdt,
                tp1=float(signal.get("tp1", 0)),
                tp2=float(signal.get("tp2", 0)),
                tp3=float(signal.get("tp3", 0)),
                stop_loss=float(signal.get("stop_loss", 0)),
                risk_reward=float(signal.get("risk_reward", 0)),
                confidence=float(signal.get("confidence", 0)),
            )
            self._positions[self._next_id] = pos
            self._next_id += 1

        logger.info(
            f"📝 PAPER TRADE OPENED  #{pos.id} "
            f"{pos.symbol} {pos.side}  size=${pos.size_usdt:.2f}  "
            f"entry={pos.entry_price}  sl={pos.stop_loss}  tp1={pos.tp1}"
        )
        return pos

    async def close_position(self, position_id: int, exit_price: float, status: str) -> Optional[PaperPosition]:
        """Close a paper position at a given price."""
        async with self._lock:
            pos = self._positions.pop(position_id, None)
            if pos is None:
                return None

            if pos.entry_price <= 0:
                pos.pnl_usdt = 0.0
                pos.pnl_pct = 0.0
            else:
                if pos.side == "LONG":
                    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
                else:
                    pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100
                pos.pnl_pct = round(pnl_pct, 2)
                pos.pnl_usdt = round(pos.size_usdt * pnl_pct / 100, 2)

            pos.status = status
            pos.closed_at = datetime.now(timezone.utc).isoformat()
            self._balance += pos.pnl_usdt
            self._history.append(pos)

        logger.info(
            f"📝 PAPER TRADE CLOSED  #{pos.id} "
            f"{pos.symbol} {pos.side}  status={status}  "
            f"pnl=${pos.pnl_usdt:+.2f} ({pos.pnl_pct:+.2f}%)  "
            f"balance=${self._balance:.2f}"
        )
        return pos

    async def get_state(self) -> PaperAccountState:
        async with self._lock:
            closed = list(self._history)
            open_pos = list(self._positions.values())

        wins = [p for p in closed if p.status in ("TP1", "TP2", "TP3")]
        losses = [p for p in closed if p.status == "SL"]
        total_pnl = sum(p.pnl_usdt for p in closed)
        win_rate = round(len(wins) / max(1, len(closed)) * 100, 1)

        return PaperAccountState(
            balance_usdt=round(self._balance, 2),
            equity_usdt=round(self._balance, 2),
            total_trades=len(closed),
            wins=len(wins),
            losses=len(losses),
            win_rate=win_rate,
            total_pnl_usdt=round(total_pnl, 2),
            total_pnl_pct=round((self._balance - self._initial_balance) / self._initial_balance * 100, 2),
            open_positions=open_pos,
            closed_positions=closed[-50:],
        )

    async def reset(self) -> None:
        async with self._lock:
            self._balance = self._initial_balance
            self._positions.clear()
            self._history.clear()
            self._next_id = 1
        logger.info(f"📝 PAPER ACCOUNT RESET — balance=${self._initial_balance:.2f}")


# Module-level singleton
paper_account = PaperAccount()
