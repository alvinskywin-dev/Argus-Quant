"""
Real historical backtest engine.

Replays Binance OHLCV klines candle-by-candle, runs the full MTF pipeline
(1D → 4H → 1H → 15M) at each 15M close, simulates TP1/TP2/TP3/SL exits on
subsequent candles, and computes full performance metrics.

Replay flow
-----------
1.  Fetch OHLCV for 15m / 1h / 4h / 1d, including warm-up bars before the
    test window so EMA200 / Supertrend etc. are stable from bar 1.
2.  Iterate each 15m candle inside the test window:
      a.  Rebuild higher-TF snapshots only when a new 1D / 4H / 1H candle
          closes (cache-and-invalidate approach for speed).
      b.  Run the live evaluate_pipeline() + build_levels() — identical code
          path to the live scanner.
      c.  On signal: scan forward candle-by-candle to find the first TP or SL
          hit within MAX_HOLD_CANDLES (conservative SL-first assumption when
          both TP and SL could hit on the same candle).
3.  Aggregate metrics: win rate, profit factor, Sharpe, max drawdown, monthly.

Performance
-----------
90-day backtest on BTCUSDT → ~8 640 15M candles → ~20-40 s in a thread pool.
Higher-TF snapshot caching + early 1D trend exit reduces full pipeline calls
to roughly 15–25% of the total candle count.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.ai_scoring.mtf import evaluate_pipeline
from app.market_data.klines import fetch_klines_historical
from app.risk.levels import build_levels
from app.strategies.features import build_snapshot
from app.utils.logger import logger

# ── tuning constants ──────────────────────────────────────────────────────────

# Bars fed to build_snapshot() at each evaluation point
_SNAP_WINDOW = 250
# Warm-up: how many extra bars of each TF to prefetch before the test window.
# 1D needs EMA200 → 250 daily bars; lower TFs need less but we use same value.
_WARMUP_BARS = 250
# Max candles to hold a trade before marking it EXPIRED (~50 h at 15M)
_MAX_HOLD = 200

# Milliseconds per interval (used to compute warm-up start time)
_INTERVAL_MS: Dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "1h":  60 * 60 * 1000,
    "4h":  4 * 60 * 60 * 1000,
    "1d":  24 * 60 * 60 * 1000,
}

WIN_STATUSES = ("TP1", "TP2", "TP3")


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class BtTrade:
    symbol: str
    side: str
    entry_price: float
    entry_time: datetime
    tp1: float
    tp2: float
    tp3: float
    sl: float
    confidence: float
    risk_reward: float
    trend_score: float = 0.0
    structure_score: float = 0.0
    setup_score: float = 0.0
    entry_score: float = 0.0
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    status: str = "OPEN"      # TP1 | TP2 | TP3 | SL | EXPIRED
    pnl_pct: float = 0.0
    hold_candles: int = 0

    def to_dict(self) -> dict:
        return {
            "symbol":          self.symbol,
            "side":            self.side,
            "entry_price":     round(self.entry_price, 6),
            "entry_time":      self.entry_time.isoformat() if self.entry_time else None,
            "exit_price":      round(self.exit_price, 6),
            "exit_time":       self.exit_time.isoformat() if self.exit_time else None,
            "status":          self.status,
            "pnl_pct":         round(self.pnl_pct, 3),
            "confidence":      round(self.confidence, 1),
            "risk_reward":     round(self.risk_reward, 2),
            "hold_candles":    self.hold_candles,
            "trend_score":     round(self.trend_score, 1),
            "structure_score": round(self.structure_score, 1),
            "setup_score":     round(self.setup_score, 1),
            "entry_score":     round(self.entry_score, 1),
        }


@dataclass
class BtResult:
    symbol: str
    start_date: str
    end_date: str
    strategy_version: str = "V3.2"
    # Scan funnel
    candles_scanned: int = 0
    signals_generated: int = 0
    # Closed-trade metrics
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    expired: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_rr: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    total_pnl: float = 0.0
    profit_factor: Optional[float] = None
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    monthly: List[dict] = field(default_factory=list)
    rr_distribution: List[dict] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)
    generated_at: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


# ── exit simulator ────────────────────────────────────────────────────────────

def _simulate_exit(
    i_start: int,
    n: int,
    side: str,
    tp1: float,
    tp2: float,
    tp3: float,
    sl: float,
    high_arr: np.ndarray,
    low_arr: np.ndarray,
    close_arr: np.ndarray,
) -> Tuple[int, str, float]:
    """
    Scan forward from i_start through at most _MAX_HOLD candles looking for
    the first TP1/TP2/TP3 or SL hit.

    Within a single candle where both SL and a TP could be touched, SL takes
    priority (pessimistic/conservative assumption).

    Returns (exit_candle_index, status, exit_price).
    """
    end = min(i_start + _MAX_HOLD, n)
    for j in range(i_start, end):
        h = high_arr[j]
        l = low_arr[j]
        if side == "LONG":
            if l <= sl:
                return j, "SL", sl
            if h >= tp3:
                return j, "TP3", tp3
            if h >= tp2:
                return j, "TP2", tp2
            if h >= tp1:
                return j, "TP1", tp1
        else:  # SHORT
            if h >= sl:
                return j, "SL", sl
            if l <= tp3:
                return j, "TP3", tp3
            if l <= tp2:
                return j, "TP2", tp2
            if l <= tp1:
                return j, "TP1", tp1
    # Neither TP nor SL hit within MAX_HOLD → expired
    exp_i = min(end, n - 1)
    return exp_i, "EXPIRED", float(close_arr[exp_i])


# ── metrics builder ───────────────────────────────────────────────────────────

def _compute_metrics(
    trades: List[BtTrade],
    symbol: str,
    start_date: str,
    end_date: str,
    strategy_version: str,
    candles_scanned: int,
    signals_generated: int,
) -> BtResult:
    res = BtResult(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        strategy_version=strategy_version,
        candles_scanned=candles_scanned,
        signals_generated=signals_generated,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if not trades:
        res.equity_curve = [0.0]
        return res

    closed = [t for t in trades if t.status != "OPEN"]
    wins   = [t for t in closed if t.status in WIN_STATUSES]
    losses = [t for t in closed if t.status == "SL"]
    exps   = [t for t in closed if t.status == "EXPIRED"]

    res.total_trades = len(closed)
    res.wins    = len(wins)
    res.losses  = len(losses)
    res.expired = len(exps)
    res.win_rate = round(len(wins) / max(1, len(closed)) * 100, 1)

    pnls = [t.pnl_pct for t in closed]
    rrs  = [t.risk_reward for t in closed]

    res.avg_pnl         = round(sum(pnls) / max(1, len(pnls)), 3)
    res.avg_rr          = round(sum(rrs)  / max(1, len(rrs)),  2)
    res.best_trade_pnl  = round(max(pnls, default=0.0), 3)
    res.worst_trade_pnl = round(min(pnls, default=0.0), 3)
    res.total_pnl       = round(sum(pnls), 3)

    gross_win  = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    res.profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # Sharpe (population std)
    if len(pnls) > 1:
        mean = sum(pnls) / len(pnls)
        std  = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls))
        res.sharpe_ratio = round(mean / max(1e-9, std), 2)

    # Equity curve + max drawdown
    cum = peak = max_dd = 0.0
    curve = [0.0]
    for p in pnls:
        cum = round(cum + p, 3)
        curve.append(cum)
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    res.equity_curve       = curve[-121:]   # max 121 points (120 trades + start)
    res.max_drawdown_pct   = round(max_dd, 3)

    # Monthly breakdown
    from collections import defaultdict
    mo_map: Dict[str, List[BtTrade]] = defaultdict(list)
    for t in closed:
        if t.entry_time:
            mo_map[t.entry_time.strftime("%Y-%m")].append(t)

    for month, mo_trades in sorted(mo_map.items()):
        mo_wins = [t for t in mo_trades if t.status in WIN_STATUSES]
        mo_pnls = [t.pnl_pct for t in mo_trades]
        mo_n    = max(1, len(mo_trades))
        mo_gw   = sum(p for p in mo_pnls if p > 0)
        mo_gl   = abs(sum(p for p in mo_pnls if p < 0))
        res.monthly.append({
            "month":         month,
            "signals":       len(mo_trades),
            "wins":          len(mo_wins),
            "losses":        len([t for t in mo_trades if t.status == "SL"]),
            "win_rate":      round(len(mo_wins) / mo_n * 100, 1),
            "total_pnl":     round(sum(mo_pnls), 3),
            "profit_factor": round(mo_gw / mo_gl, 2) if mo_gl > 0 else None,
        })

    # RR distribution
    from collections import Counter
    rr_buckets: Counter = Counter()
    for rr in rrs:
        b = f"{math.floor(rr * 2) / 2:.1f}"
        rr_buckets[b] += 1
    res.rr_distribution = sorted(
        [{"rr": k, "count": v} for k, v in rr_buckets.items()],
        key=lambda x: float(x["rr"]),
    )

    # Trades list (newest first, capped at 200)
    res.trades = [t.to_dict() for t in reversed(closed[-200:])]

    return res


# ── main engine ───────────────────────────────────────────────────────────────

class HistoricalBacktestEngine:
    """
    True candle-replay backtest engine.

    Usage::

        engine = HistoricalBacktestEngine()
        result = await engine.run("BTCUSDT", "2025-01-01", "2025-03-31")
        print(result.to_dict())
    """

    # ── public API ────────────────────────────────────────────────────────────

    async def run(
        self,
        symbol: str,
        start_str: str,
        end_str: str,
        strategy_version: str = "V3.2",
    ) -> BtResult:
        """
        Fetch data and run historical simulation for one symbol.

        Parameters
        ----------
        symbol        : Binance futures pair, e.g. "BTCUSDT"
        start_str     : ISO date string "YYYY-MM-DD" (test window start, UTC)
        end_str       : ISO date string "YYYY-MM-DD" (test window end, UTC, inclusive)
        strategy_version : label stored in the result (UI only)
        """
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_dt   = (
                datetime.strptime(end_str,   "%Y-%m-%d").replace(tzinfo=timezone.utc)
                + timedelta(days=1)
            )
        except ValueError as exc:
            return BtResult(
                symbol=symbol, start_date=start_str, end_date=end_str,
                error=f"bad date format: {exc}",
            )

        if end_dt <= start_dt:
            return BtResult(
                symbol=symbol, start_date=start_str, end_date=end_str,
                error="end_date must be after start_date",
            )

        if (end_dt - start_dt).days > 366:
            return BtResult(
                symbol=symbol, start_date=start_str, end_date=end_str,
                error="date range exceeds 366 days maximum",
            )

        logger.info(
            f"backtest start  symbol={symbol} "
            f"{start_str}→{end_str} strategy={strategy_version}"
        )

        try:
            dfs = await self._fetch_all(symbol.upper(), start_dt, end_dt)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"backtest data fetch failed: {exc}")
            return BtResult(
                symbol=symbol, start_date=start_str, end_date=end_str,
                error=f"data fetch failed: {exc}",
            )

        # CPU-bound replay runs in a thread pool so the event loop stays free
        loop = asyncio.get_event_loop()
        result: BtResult = await loop.run_in_executor(
            None,
            lambda: self._replay(
                symbol.upper(), start_dt, end_dt, dfs, strategy_version
            ),
        )
        logger.info(
            f"backtest done   symbol={symbol} "
            f"trades={result.total_trades} wr={result.win_rate}%"
        )
        return result

    # ── data fetching ─────────────────────────────────────────────────────────

    async def _fetch_all(
        self,
        symbol: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch all four TF DataFrames with warm-up bars prepended."""
        end_ms = int(end_dt.timestamp() * 1000)

        async def _fetch(interval: str) -> pd.DataFrame:
            warmup_ms = _WARMUP_BARS * _INTERVAL_MS[interval]
            fetch_start_ms = int(start_dt.timestamp() * 1000) - warmup_ms
            df = await fetch_klines_historical(symbol, interval, fetch_start_ms, end_ms)
            return df

        dfs_list = await asyncio.gather(
            _fetch("15m"),
            _fetch("1h"),
            _fetch("4h"),
            _fetch("1d"),
        )
        return {"15m": dfs_list[0], "1h": dfs_list[1], "4h": dfs_list[2], "1d": dfs_list[3]}

    # ── synchronous replay (runs in executor) ─────────────────────────────────

    def _replay(
        self,
        symbol: str,
        start_dt: datetime,
        end_dt: datetime,
        dfs: Dict[str, pd.DataFrame],
        strategy_version: str,
    ) -> BtResult:
        m15_df = dfs.get("15m", pd.DataFrame())
        h1_df  = dfs.get("1h",  pd.DataFrame())
        h4_df  = dfs.get("4h",  pd.DataFrame())
        d1_df  = dfs.get("1d",  pd.DataFrame())

        _empty = BtResult(
            symbol=symbol,
            start_date=start_dt.strftime("%Y-%m-%d"),
            end_date=(end_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
            strategy_version=strategy_version,
            equity_curve=[0.0],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        if m15_df.empty or h1_df.empty or h4_df.empty or d1_df.empty:
            _empty.error = "one or more timeframes returned no data"
            return _empty

        # ── Pre-extract numpy arrays for fast indexed access ───────────────
        m15_close_times = m15_df["close_time"].values   # numpy datetime64
        h1_close_times  = h1_df["close_time"].values
        h4_close_times  = h4_df["close_time"].values
        d1_close_times  = d1_df["close_time"].values

        m15_high_arr   = m15_df["high"].values.astype(float)
        m15_low_arr    = m15_df["low"].values.astype(float)
        m15_close_arr  = m15_df["close"].values.astype(float)
        m15_open_times = m15_df["open_time"].values

        n15 = len(m15_df)

        # ── Locate test-window boundaries in 15M index ─────────────────────
        start_ts64 = np.datetime64(start_dt.replace(tzinfo=None), "ms")
        end_ts64   = np.datetime64(end_dt.replace(tzinfo=None),   "ms")

        # First 15M candle that CLOSES on or after start_dt
        raw_start_i = int(np.searchsorted(m15_close_times, start_ts64, side="left"))
        # Ensure we have enough warm-up history
        start_i = max(raw_start_i, _SNAP_WINDOW)
        end_i   = int(np.searchsorted(m15_close_times, end_ts64, side="left"))
        end_i   = min(end_i, n15)

        if start_i >= end_i:
            _empty.error = "test window too narrow or no data in range"
            return _empty

        # ── Snapshot cache (rebuild only when TF candle closes) ────────────
        cur_d1_idx = cur_h4_idx = cur_h1_idx = -1
        snap_d1 = snap_h4 = snap_h1 = None

        trades: List[BtTrade] = []
        active_trade_end_i = -1   # resume signal search after this index

        candles_scanned = signals_generated = 0

        for i15 in range(start_i, end_i):
            candles_scanned += 1

            # Skip while we're holding an open trade
            if i15 <= active_trade_end_i:
                continue

            ts = m15_close_times[i15]

            # ── Rebuild 1D snapshot when new 1D candle closes ──────────────
            new_d1_idx = int(np.searchsorted(d1_close_times, ts, side="right"))
            if new_d1_idx != cur_d1_idx:
                cur_d1_idx = new_d1_idx
                if new_d1_idx >= 60:
                    sl = d1_df.iloc[max(0, new_d1_idx - _SNAP_WINDOW): new_d1_idx]
                    snap_d1 = build_snapshot(symbol, "1d", sl.reset_index(drop=True))
                else:
                    snap_d1 = None

            if snap_d1 is None:
                continue

            # Fast 1D trend pre-check — skip 4H/1H/15M builds if trend absent
            d1_bull = snap_d1.ema_50 > snap_d1.ema_200 and snap_d1.last_close > snap_d1.ema_200
            d1_bear = snap_d1.ema_50 < snap_d1.ema_200 and snap_d1.last_close < snap_d1.ema_200
            if not d1_bull and not d1_bear:
                continue

            # ── Rebuild 4H snapshot when new 4H candle closes ─────────────
            new_h4_idx = int(np.searchsorted(h4_close_times, ts, side="right"))
            if new_h4_idx != cur_h4_idx:
                cur_h4_idx = new_h4_idx
                if new_h4_idx >= 60:
                    sl = h4_df.iloc[max(0, new_h4_idx - _SNAP_WINDOW): new_h4_idx]
                    snap_h4 = build_snapshot(symbol, "4h", sl.reset_index(drop=True))
                else:
                    snap_h4 = None

            if snap_h4 is None:
                continue

            # ── Rebuild 1H snapshot when new 1H candle closes ─────────────
            new_h1_idx = int(np.searchsorted(h1_close_times, ts, side="right"))
            if new_h1_idx != cur_h1_idx:
                cur_h1_idx = new_h1_idx
                if new_h1_idx >= 60:
                    sl = h1_df.iloc[max(0, new_h1_idx - _SNAP_WINDOW): new_h1_idx]
                    snap_h1 = build_snapshot(symbol, "1h", sl.reset_index(drop=True))
                else:
                    snap_h1 = None

            if snap_h1 is None:
                continue

            # ── Build 15M snapshot every qualifying candle ─────────────────
            s15 = m15_df.iloc[max(0, i15 - _SNAP_WINDOW + 1): i15 + 1]
            snap_15m = build_snapshot(symbol, "15m", s15.reset_index(drop=True))
            if snap_15m is None:
                continue

            # ── Run full MTF pipeline ──────────────────────────────────────
            snaps = {"1d": snap_d1, "4h": snap_h4, "1h": snap_h1, "15m": snap_15m}
            decision, _rejection = evaluate_pipeline(snaps)
            if decision is None:
                continue

            levels = build_levels(snap_15m, decision.side)
            if levels is None:
                continue

            signals_generated += 1

            # ── Create trade record ────────────────────────────────────────
            entry_price = float(m15_close_arr[i15])

            raw_ts = m15_open_times[i15]
            if isinstance(raw_ts, (int, np.integer)):
                entry_time = datetime.fromtimestamp(int(raw_ts) / 1e9, tz=timezone.utc)
            else:
                entry_time = pd.Timestamp(raw_ts).to_pydatetime().replace(tzinfo=timezone.utc)

            trade = BtTrade(
                symbol=symbol,
                side=decision.side,
                entry_price=entry_price,
                entry_time=entry_time,
                tp1=levels.tp1,
                tp2=levels.tp2,
                tp3=levels.tp3,
                sl=levels.stop_loss,
                confidence=decision.confidence,
                risk_reward=levels.risk_reward,
                trend_score=decision.trend_score,
                structure_score=decision.structure_score,
                setup_score=decision.setup_score,
                entry_score=decision.entry_score_pts,
            )

            # ── Simulate TP/SL exit ────────────────────────────────────────
            exit_i, exit_status, exit_price = _simulate_exit(
                i15 + 1, n15,
                decision.side,
                levels.tp1, levels.tp2, levels.tp3, levels.stop_loss,
                m15_high_arr, m15_low_arr, m15_close_arr,
            )

            raw_exit_ts = m15_open_times[exit_i]
            if isinstance(raw_exit_ts, (int, np.integer)):
                exit_time = datetime.fromtimestamp(int(raw_exit_ts) / 1e9, tz=timezone.utc)
            else:
                exit_time = pd.Timestamp(raw_exit_ts).to_pydatetime().replace(tzinfo=timezone.utc)

            trade.exit_price   = exit_price
            trade.exit_time    = exit_time
            trade.status       = exit_status
            trade.hold_candles = exit_i - i15

            if decision.side == "LONG":
                trade.pnl_pct = round((exit_price - entry_price) / entry_price * 100, 3)
            else:
                trade.pnl_pct = round((entry_price - exit_price) / entry_price * 100, 3)

            trades.append(trade)
            active_trade_end_i = exit_i   # block new signals until trade closes

        return _compute_metrics(
            trades, symbol,
            start_dt.strftime("%Y-%m-%d"),
            (end_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
            strategy_version,
            candles_scanned,
            signals_generated,
        )
