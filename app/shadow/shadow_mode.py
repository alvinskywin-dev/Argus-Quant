"""
Sprint 22G — Shadow Mode Live Validation.

Answers "what would have happened if this signal executed live?" WITHOUT ever
touching the exchange. It simulates entry, TP/SL placement, latency and
slippage, then reconciles the hypothetical against actual market movement so we
can compare paper vs. (hypothetical) live vs. reality before opening the live
gate.

╔══════════════════════════════════════════════════════════════════╗
║  HARD SAFETY GUARANTEE                                            ║
║  This module imports NO exchange client, places NO orders, sends ║
║  NO execution requests, and modifies NO exchange state. It is    ║
║  arithmetic over price data only. There is intentionally no code ║
║  path from here to an adapter.                                    ║
╚══════════════════════════════════════════════════════════════════╝

With `SHADOW_MODE_ENABLED=false` the caller should skip it; the functions
themselves are pure and safe to call regardless.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean
from typing import Optional, Sequence

from app.config import settings

# Belt-and-braces marker asserted by the safety test: this module must never
# import an exchange adapter / client.
__places_real_orders__ = False


@dataclass
class ShadowFill:
    requested_price: float
    fill_price: float
    slippage_percent: float
    latency_ms: float
    filled: bool = True
    miss_probability: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ShadowResult:
    symbol: str
    side: str
    hypothetical_entry: float
    hypothetical_tp: float
    hypothetical_sl: float
    outcome: str = "OPEN"  # TP | SL | OPEN
    hypothetical_pnl_percent: float = 0.0
    entry_fill: Optional[ShadowFill] = None
    execution_latency_ms: float = 0.0
    slippage_estimate_percent: float = 0.0
    missed_fill_probability: float = 0.0
    tp_sl_sync_ok: bool = True
    # comparison
    paper_pnl_percent: Optional[float] = None
    actual_move_percent: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.entry_fill is not None:
            d["entry_fill"] = self.entry_fill.to_dict()
        return d


def _slippage_pct() -> float:
    return max(0.0, settings.shadow_mode_slippage_bps) / 100.0  # bps -> percent


def simulate_entry_fill(
    side: str,
    requested_price: float,
    *,
    next_candle: Optional[dict] = None,
) -> ShadowFill:
    """Simulate the entry fill with slippage + latency. NEVER places an order.

    Slippage always works against us. ``next_candle`` (the bar after signal) is
    used to estimate miss probability: if price gapped away from a limit entry
    the fill may be missed.
    """
    slip = _slippage_pct()
    side_u = (side or "").upper()
    # Adverse slippage: pay up for LONG, sell lower for SHORT.
    fill = (
        requested_price * (1 + slip / 100.0)
        if side_u == "LONG"
        else requested_price * (1 - slip / 100.0)
    )

    miss_prob = 0.0
    filled = True
    if next_candle:
        lo = float(next_candle.get("low", requested_price))
        hi = float(next_candle.get("high", requested_price))
        # A limit entry needs price to trade through it.
        if side_u == "LONG" and lo > requested_price:
            miss_prob = min(1.0, (lo - requested_price) / requested_price / max(slip / 100.0, 1e-6))
        elif side_u == "SHORT" and hi < requested_price:
            miss_prob = min(1.0, (requested_price - hi) / requested_price / max(slip / 100.0, 1e-6))
        filled = miss_prob < 0.5

    return ShadowFill(
        requested_price=round(requested_price, 10),
        fill_price=round(fill, 10),
        slippage_percent=(
            round(abs(fill - requested_price) / requested_price * 100.0, 5)
            if requested_price
            else 0.0
        ),
        latency_ms=float(settings.shadow_mode_latency_ms),
        filled=filled,
        miss_probability=round(miss_prob, 4),
    )


def simulate_signal(
    signal: dict,
    *,
    price_path: Optional[Sequence[float]] = None,
    paper_pnl_percent: Optional[float] = None,
    next_candle: Optional[dict] = None,
) -> ShadowResult:
    """Run a full hypothetical-live simulation for one signal.

    ``signal`` keys: symbol, side/direction, entry (or entry_low/high mid),
    tp1, stop_loss. ``price_path`` is the post-entry price sequence used to
    resolve TP/SL and the actual market move.
    """
    side = str(signal.get("side", signal.get("direction", "LONG"))).upper()
    symbol = str(signal.get("symbol", ""))

    entry = signal.get("entry")
    if entry is None:
        lo, hi = signal.get("entry_low"), signal.get("entry_high")
        entry = (float(lo) + float(hi)) / 2 if (lo is not None and hi is not None) else 0.0
    entry = float(entry or 0.0)
    tp = float(signal.get("tp1") or 0.0)
    sl = float(signal.get("stop_loss") or 0.0)

    fill = simulate_entry_fill(side, entry, next_candle=next_candle)
    eff_entry = fill.fill_price

    outcome = "OPEN"
    exit_price = None
    if price_path and eff_entry:
        for p in price_path:
            p = float(p)
            if side == "LONG":
                if sl and p <= sl:
                    outcome, exit_price = "SL", sl
                    break
                if tp and p >= tp:
                    outcome, exit_price = "TP", tp
                    break
            else:  # SHORT
                if sl and p >= sl:
                    outcome, exit_price = "SL", sl
                    break
                if tp and p <= tp:
                    outcome, exit_price = "TP", tp
                    break

    # Hypothetical PnL with adverse exit slippage too.
    pnl = 0.0
    if exit_price is not None and eff_entry:
        slip = _slippage_pct() / 100.0
        exit_eff = exit_price * (1 - slip) if side == "LONG" else exit_price * (1 + slip)
        raw = (exit_eff - eff_entry) / eff_entry * 100.0
        pnl = raw if side == "LONG" else -raw
    elif price_path and eff_entry:
        last = float(price_path[-1])
        raw = (last - eff_entry) / eff_entry * 100.0
        pnl = raw if side == "LONG" else -raw

    actual_move = None
    if price_path and entry:
        last = float(price_path[-1])
        actual_move = round((last - entry) / entry * 100.0, 4)

    # TP/SL sync realism: both must be on the correct side of entry.
    tp_sl_ok = True
    if tp and sl and eff_entry:
        if side == "LONG":
            tp_sl_ok = tp > eff_entry > sl
        else:
            tp_sl_ok = tp < eff_entry < sl

    return ShadowResult(
        symbol=symbol,
        side=side,
        hypothetical_entry=round(eff_entry, 10),
        hypothetical_tp=round(tp, 10),
        hypothetical_sl=round(sl, 10),
        outcome=outcome,
        hypothetical_pnl_percent=round(pnl, 4),
        entry_fill=fill,
        execution_latency_ms=fill.latency_ms,
        slippage_estimate_percent=fill.slippage_percent,
        missed_fill_probability=fill.miss_probability,
        tp_sl_sync_ok=tp_sl_ok,
        paper_pnl_percent=paper_pnl_percent,
        actual_move_percent=actual_move,
    )


@dataclass
class ShadowReport:
    sample_size: int = 0
    shadow_winrate: float = 0.0
    paper_winrate: Optional[float] = None
    avg_slippage_percent: float = 0.0
    avg_latency_ms: float = 0.0
    avg_missed_fill_probability: float = 0.0
    slippage_impact_percent: float = 0.0  # paper PnL vs shadow PnL delta
    tp_sl_sync_realism: float = 0.0  # fraction with valid sync
    avg_shadow_pnl_percent: float = 0.0
    avg_paper_pnl_percent: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_report(results: Sequence[ShadowResult]) -> ShadowReport:
    """Aggregate shadow results into a dashboard report (shadow vs paper)."""
    rs = list(results)
    if not rs:
        return ShadowReport()

    wins = [r for r in rs if r.outcome == "TP"]
    losses = [r for r in rs if r.outcome == "SL"]
    decided = len(wins) + len(losses)
    shadow_wr = round(len(wins) / decided * 100.0, 2) if decided else 0.0

    paper_pnls = [r.paper_pnl_percent for r in rs if r.paper_pnl_percent is not None]
    shadow_pnls = [r.hypothetical_pnl_percent for r in rs]
    paper_wins = [p for p in paper_pnls if p > 0]
    paper_wr = round(len(paper_wins) / len(paper_pnls) * 100.0, 2) if paper_pnls else None

    slip_impact = 0.0
    if paper_pnls:
        # mean paper PnL minus mean shadow PnL over the same set with paper data.
        paired = [
            (r.paper_pnl_percent, r.hypothetical_pnl_percent)
            for r in rs
            if r.paper_pnl_percent is not None
        ]
        if paired:
            slip_impact = round(mean(p for p, _ in paired) - mean(s for _, s in paired), 4)

    return ShadowReport(
        sample_size=len(rs),
        shadow_winrate=shadow_wr,
        paper_winrate=paper_wr,
        avg_slippage_percent=round(mean(r.slippage_estimate_percent for r in rs), 5),
        avg_latency_ms=round(mean(r.execution_latency_ms for r in rs), 2),
        avg_missed_fill_probability=round(mean(r.missed_fill_probability for r in rs), 4),
        slippage_impact_percent=slip_impact,
        tp_sl_sync_realism=round(sum(1 for r in rs if r.tp_sl_sync_ok) / len(rs), 4),
        avg_shadow_pnl_percent=round(mean(shadow_pnls), 4),
        avg_paper_pnl_percent=round(mean(paper_pnls), 4) if paper_pnls else None,
    )


def is_enabled() -> bool:
    return bool(settings.shadow_mode_enabled)
