"""
Track open signals — poll prices, mark TP1/TP2/TP3/SL hits, compute PnL.

Lives separately from the scanner so it can run on its own cadence and survive
restarts (state is persisted in PostgreSQL).
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List

from app.analytics.trade_outcome import classify_trade_outcome, record_exit_event
from app.database import repo
from app.database.models import Signal
from app.market_data.binance_client import get_client
from app.utils.helpers import utcnow
from app.utils.logger import logger


class SignalTracker:
    def __init__(self, poll_sec: int = 30) -> None:
        self.poll_sec = poll_sec
        self._update_callbacks: list = []

    def on_update(self, cb) -> None:
        """Register a callback(signal_dict, event_type) for TP/SL events."""
        self._update_callbacks.append(cb)

    async def _fanout(self, sig: Signal, event: str) -> None:
        outcome = classify_trade_outcome(
            status=sig.status,
            realized_pnl=sig.pnl_pct,
            diagnostics=sig.diagnostics,
        )
        payload = {
            "id": sig.id,
            "signal_id": sig.id,
            "symbol": sig.symbol,
            "side": sig.side,
            "event": event,  # TP1/TP2/TP3/SL/UPDATE
            "pnl_pct": sig.pnl_pct,
            "telegram_message_id": sig.telegram_message_id,
            # Display-only: lets the Telegram formatter show holding time.
            "opened_at": sig.created_at,
            "event_time": utcnow(),
            # Lifecycle-aware outcome so Telegram can render TP-then-SL correctly.
            "trade_outcome": outcome.outcome,
            "winrate_bucket": outcome.winrate_bucket,
            "max_tp_hit": outcome.max_tp_hit,
        }
        for cb in self._update_callbacks:
            try:
                await cb(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"tracker callback failed: {exc}")

    async def _prices(self, symbols: List[str]) -> Dict[str, float]:
        client = await get_client()
        try:
            data = await client.book_ticker()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"bookTicker failed: {exc}")
            return {}
        wanted = set(symbols)
        out: Dict[str, float] = {}
        for row in data:
            sym = row.get("symbol")
            if sym in wanted:
                bid = float(row.get("bidPrice", 0) or 0)
                ask = float(row.get("askPrice", 0) or 0)
                if bid > 0 and ask > 0:
                    out[sym] = (bid + ask) / 2
        return out

    @staticmethod
    def _pnl_pct(sig: Signal, price: float) -> float:
        entry = (sig.entry_low + sig.entry_high) / 2
        if sig.side == "LONG":
            return (price - entry) / entry * 100.0
        return (entry - price) / entry * 100.0

    async def _check_one(self, sig: Signal, price: float) -> None:
        pnl = self._pnl_pct(sig, price)
        max_fav = max(sig.max_favorable_pct, pnl)
        max_adv = min(sig.max_adverse_pct, pnl)

        fields: dict = {
            "pnl_pct": round(pnl, 3),
            "max_favorable_pct": round(max_fav, 3),
            "max_adverse_pct": round(max_adv, 3),
        }

        new_status = sig.status
        event = None

        if sig.side == "LONG":
            if price <= sig.stop_loss and sig.status != "SL":
                new_status, event = "SL", "SL"
            elif price >= sig.tp3 and sig.status not in {"SL"}:
                new_status, event = "TP3", "TP3"
            elif price >= sig.tp2 and sig.status not in {"SL", "TP2", "TP3"}:
                new_status, event = "TP2", "TP2"
            elif price >= sig.tp1 and sig.status not in {"SL", "TP1", "TP2", "TP3"}:
                new_status, event = "TP1", "TP1"
        else:
            if price >= sig.stop_loss and sig.status != "SL":
                new_status, event = "SL", "SL"
            elif price <= sig.tp3 and sig.status not in {"SL"}:
                new_status, event = "TP3", "TP3"
            elif price <= sig.tp2 and sig.status not in {"SL", "TP2", "TP3"}:
                new_status, event = "TP2", "TP2"
            elif price <= sig.tp1 and sig.status not in {"SL", "TP1", "TP2", "TP3"}:
                new_status, event = "TP1", "TP1"

        if event:
            now = utcnow()
            fields["status"] = new_status
            if event in {"TP3", "SL"}:
                fields["closed_at"] = now
            # Persist lifecycle (tp_history) into diagnostics so a later SL never
            # erases the fact that a take-profit was reached. Display-only +
            # winrate; does not change signal/execution logic.
            updated_diag = record_exit_event(
                sig.diagnostics, event, event_time=now, realized_pnl=fields["pnl_pct"]
            )
            fields["diagnostics"] = json.dumps(updated_diag)
            sig.status = new_status
            sig.pnl_pct = fields["pnl_pct"]
            sig.diagnostics = fields["diagnostics"]
            await repo.update_signal(sig.id, fields)
            await self._fanout(sig, event)
            logger.info(f"signal #{sig.id} {sig.symbol} -> {event} pnl={pnl:+.2f}%")
        else:
            await repo.update_signal(sig.id, fields)

    async def run_forever(self) -> None:
        while True:
            try:
                opens = await repo.get_open_signals()
                if opens:
                    syms = list({s.symbol for s in opens})
                    prices = await self._prices(syms)
                    for sig in opens:
                        price = prices.get(sig.symbol)
                        if price:
                            await self._check_one(sig, price)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"tracker loop error: {exc}")
            await asyncio.sleep(self.poll_sec)
