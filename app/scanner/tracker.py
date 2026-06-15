"""
Track open signals — poll prices, mark TP1/TP2/TP3/SL hits, compute PnL.

Lives separately from the scanner so it can run on its own cadence and survive
restarts (state is persisted in PostgreSQL).

Fidelity upgrades (trading-logic audit):
  * Lifecycle PnL — a take-profit books a partial and moves the stop to
    break-even, so a TP-then-SL trade records the blended realized PnL instead
    of a full loss (pnl_pct then agrees with the lifecycle win-rate).
  * Wick detection — TP/SL touches are read from the 1m candle high/low between
    polls, not just the latest mid price, so a wick that tags a level counts.
  * Entry fill — a signal is only "filled" once price actually trades inside its
    entry band; one that never returns is expired rather than counted as a
    phantom fill.
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional, Tuple

from app.analytics.lifecycle_pnl import blended_realized_pnl
from app.analytics.trade_outcome import classify_trade_outcome, record_exit_event
from app.config import settings
from app.database import repo
from app.database.models import Signal
from app.market_data.binance_client import get_client
from app.utils.helpers import utcnow
from app.utils.logger import logger

_TP_RANK = {"TP1": 1, "TP2": 2}


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

    async def _extremes(self, symbols: List[str]) -> Dict[str, Tuple[float, float]]:
        """Return {symbol: (high, low)} over the most recent 1m candles so that a
        TP/SL wick between two polls is not missed. Best-effort: a symbol missing
        from the result simply falls back to the latest mid price."""
        client = await get_client()

        async def one(sym: str) -> Optional[Tuple[str, float, float]]:
            try:
                rows = await client.klines(sym, "1m", limit=2)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"klines failed for {sym}: {exc}")
                return None
            if not rows:
                return None
            # kline row: [open_time, open, high, low, close, ...]
            high = max(float(r[2]) for r in rows)
            low = min(float(r[3]) for r in rows)
            return (sym, high, low)

        out: Dict[str, Tuple[float, float]] = {}
        for res in await asyncio.gather(*(one(s) for s in set(symbols))):
            if res is not None:
                out[res[0]] = (res[1], res[2])
        return out

    @staticmethod
    def _entry(sig: Signal) -> float:
        return (sig.entry_low + sig.entry_high) / 2

    @classmethod
    def _pnl_pct(cls, sig: Signal, price: float) -> float:
        entry = cls._entry(sig)
        if sig.side == "LONG":
            return (price - entry) / entry * 100.0
        return (entry - price) / entry * 100.0

    @staticmethod
    def _diag_dict(sig: Signal) -> dict:
        if not sig.diagnostics:
            return {}
        try:
            d = json.loads(sig.diagnostics)
            return d if isinstance(d, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _tp_index(self, sig: Signal, touch: float) -> int:
        """Highest take-profit level reached by *touch* (0..3)."""
        if sig.side == "LONG":
            if touch >= sig.tp3:
                return 3
            if touch >= sig.tp2:
                return 2
            if touch >= sig.tp1:
                return 1
        else:
            if touch <= sig.tp3:
                return 3
            if touch <= sig.tp2:
                return 2
            if touch <= sig.tp1:
                return 1
        return 0

    async def _expire_unfilled(self, sig: Signal, now) -> None:
        diag = self._diag_dict(sig)
        diag["entry_fill"] = {"filled": False, "expired_at": now.isoformat()}
        fields = {
            "status": "EXPIRED",
            "closed_at": now,
            "pnl_pct": 0.0,
            "diagnostics": json.dumps(diag),
        }
        await repo.update_signal(sig.id, fields)
        logger.info(f"signal #{sig.id} {sig.symbol} -> EXPIRED (entry never filled)")

    async def _check_one(
        self, sig: Signal, price: float, extreme: Optional[Tuple[float, float]]
    ) -> None:
        now = utcnow()
        diag = self._diag_dict(sig)
        entry = self._entry(sig)

        # Favorable / adverse touch prices for this interval. With candle extremes
        # the favorable side uses the high (LONG) / low (SHORT) and vice-versa; a
        # TP recorded before SL within the same candle is the intended lifecycle.
        if extreme and settings.tracker_use_candle_extremes:
            high, low = extreme
            tp_touch, sl_touch = (high, low) if sig.side == "LONG" else (low, high)
        else:
            tp_touch = sl_touch = price

        # ── Entry-fill gating ────────────────────────────────────────────
        # Only treat the signal as live once price has actually traded inside the
        # entry band; otherwise it is a phantom fill. An order that never returns
        # to its entry within the timeout is expired (not counted as a trade).
        fill = diag.get("entry_fill") if isinstance(diag.get("entry_fill"), dict) else {}
        if settings.entry_fill_required and not fill.get("filled"):
            if extreme and settings.tracker_use_candle_extremes:
                touched = (low <= sig.entry_high) and (high >= sig.entry_low)
            else:
                touched = sig.entry_low <= price <= sig.entry_high
            if touched:
                diag["entry_fill"] = {"filled": True, "filled_at": now.isoformat()}
                sig.diagnostics = json.dumps(diag)
                await repo.update_signal(sig.id, {"diagnostics": sig.diagnostics})
            else:
                age_min = (now - sig.created_at).total_seconds() / 60.0
                if age_min >= settings.entry_fill_timeout_min:
                    await self._expire_unfilled(sig, now)
                # Not filled yet → no PnL, no level checks.
                return

        # ── Mark-to-market + MFE/MAE (use the candle extremes when available) ──
        pnl = self._pnl_pct(sig, price)
        if extreme and settings.tracker_use_candle_extremes:
            high, low = extreme
            pnl_hi, pnl_lo = self._pnl_pct(sig, high), self._pnl_pct(sig, low)
            cand_fav = max(pnl, pnl_hi, pnl_lo)
            cand_adv = min(pnl, pnl_hi, pnl_lo)
        else:
            cand_fav = cand_adv = pnl
        max_fav = max(sig.max_favorable_pct, cand_fav)
        max_adv = min(sig.max_adverse_pct, cand_adv)

        # ── Level detection ──────────────────────────────────────────────
        pre_rank = _TP_RANK.get(sig.status, 0)
        new_tp = self._tp_index(sig, tp_touch)

        # After TP1 the stop is moved to break-even; until then the real stop is
        # in force. Evaluated against the status that held at the start of the poll.
        be_active = pre_rank >= 1 and settings.move_sl_to_breakeven_after_tp1
        eff_stop = entry if be_active else sig.stop_loss
        sl_hit = (
            sl_touch <= eff_stop if sig.side == "LONG" else sl_touch >= eff_stop
        )

        # Build the event sequence (TP advance first, then SL) for this poll.
        seq: List[str] = []
        if new_tp >= 3:
            seq = ["TP3"]  # terminal win — SL on the booked remainder is moot
        else:
            if new_tp > pre_rank:
                seq.append(f"TP{new_tp}")
            if sl_hit:
                seq.append("SL")

        if not seq:
            # ── #8: skip the DB write when nothing moved materially ──
            fields = {
                "pnl_pct": round(pnl, 3),
                "max_favorable_pct": round(max_fav, 3),
                "max_adverse_pct": round(max_adv, 3),
            }
            if (
                abs(pnl - (sig.pnl_pct or 0.0)) < settings.tracker_min_pnl_delta_pct
                and round(max_fav, 3) == round(sig.max_favorable_pct or 0.0, 3)
                and round(max_adv, 3) == round(sig.max_adverse_pct or 0.0, 3)
            ):
                return
            await repo.update_signal(sig.id, fields)
            return

        # ── Apply the lifecycle events ───────────────────────────────────
        for event in seq:
            terminal = event in {"TP3", "SL"}
            if terminal and settings.lifecycle_pnl_enabled:
                final_max_tp = max(pre_rank, new_tp, 3 if event == "TP3" else 0)
                realized = blended_realized_pnl(
                    side=sig.side,
                    entry=entry,
                    tp1=sig.tp1,
                    tp2=sig.tp2,
                    tp3=sig.tp3,
                    stop_loss=sig.stop_loss,
                    max_tp_hit=final_max_tp,
                    final_event=event,
                    tp1_frac=settings.tp1_close_fraction,
                    tp2_frac=settings.tp2_close_fraction,
                    sl_to_breakeven_after_tp1=settings.move_sl_to_breakeven_after_tp1,
                )
            elif terminal:
                realized = round(self._pnl_pct(sig, eff_stop if event == "SL" else sig.tp3), 3)
            else:
                # Intermediate TP — position still open, keep mark-to-market.
                realized = round(pnl, 3)

            fields: dict = {
                "status": event,
                "pnl_pct": realized,
                "max_favorable_pct": round(max_fav, 3),
                "max_adverse_pct": round(max_adv, 3),
            }
            if terminal:
                fields["closed_at"] = now
            diag = record_exit_event(
                diag, event, event_time=now, realized_pnl=realized
            )
            fields["diagnostics"] = json.dumps(diag)

            sig.status = event
            sig.pnl_pct = realized
            sig.diagnostics = fields["diagnostics"]
            sig.max_favorable_pct = max_fav
            sig.max_adverse_pct = max_adv
            await repo.update_signal(sig.id, fields)
            await self._fanout(sig, event)
            logger.info(f"signal #{sig.id} {sig.symbol} -> {event} pnl={realized:+.2f}%")

    async def run_forever(self) -> None:
        while True:
            try:
                opens = await repo.get_open_signals()
                if opens:
                    syms = list({s.symbol for s in opens})
                    prices = await self._prices(syms)
                    extremes = (
                        await self._extremes(syms)
                        if settings.tracker_use_candle_extremes
                        else {}
                    )
                    for sig in opens:
                        price = prices.get(sig.symbol)
                        if price:
                            await self._check_one(sig, price, extremes.get(sig.symbol))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"tracker loop error: {exc}")
            await asyncio.sleep(self.poll_sec)
