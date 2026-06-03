"""analytics router — extracted from server.py (Phase 4).

Handlers moved verbatim; shared helpers/views/templates are imported
from app.dashboard.server. Wired via create_app().include_router().
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy import func as _sqlfunc

from app.dashboard.server import (
    _MTF_STRATEGY,
    _MTF_TIMEFRAMES,
    _SETUP_LIBRARY,
    _cache_get,
    _cache_set,
    _compute_backtest,
)
from app.database.models import FundingRateSnapshot, Signal
from app.database.session import SessionLocal

router = APIRouter()


@router.get("/api/public/performance")
async def api_public_performance():
    """
    Full MTF-only performance metrics.
    Filters: strategy=MTF_SMC_STRICT, timeframe IN (15m/1h/4h/1d).
    Legacy 5m signals, archived signals, and other strategies are excluded.
    """
    from collections import defaultdict as _dd

    try:
        # ── fetch all closed MTF signals (no row cap) ─────────────────
        async with SessionLocal() as session:
            closed_res = await session.execute(
                select(Signal)
                .where(
                    Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
                .order_by(Signal.created_at)
            )
            closed = list(closed_res.scalars().all())

            open_cnt_res = await session.execute(
                select(_sqlfunc.count(Signal.id)).where(
                    Signal.status == "OPEN",
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
            )
            open_count = int(open_cnt_res.scalar() or 0)

        # ── helpers ────────────────────────────────────────────────────
        WIN_ST = ("TP1", "TP2", "TP3")

        def _pf(pnls: list) -> float | None:
            gw = sum(p for p in pnls if p > 0)
            gl = abs(sum(p for p in pnls if p < 0))
            return round(gw / gl, 2) if gl > 0 else None

        def _side_stat(sigs: list) -> dict:
            sw = [s for s in sigs if s.status in WIN_ST]
            sl = [s for s in sigs if s.status == "SL"]
            sp = [float(s.pnl_pct or 0) for s in sigs]
            sr = [float(s.risk_reward or 0) for s in sigs]
            n = max(1, len(sigs))
            return {
                "total": len(sigs),
                "wins": len(sw),
                "losses": len(sl),
                "win_rate": round(len(sw) / n * 100, 1),
                "avg_pnl": round(sum(sp) / n, 2),
                "avg_rr": round(sum(sr) / n, 2),
            }

        # ── overall metrics ────────────────────────────────────────────
        total_closed = len(closed)
        wins = [s for s in closed if s.status in WIN_ST]
        losses = [s for s in closed if s.status == "SL"]
        pnls = [float(s.pnl_pct or 0) for s in closed]
        rrs = [float(s.risk_reward or 0) for s in closed]
        n = max(1, total_closed)

        win_rate = round(len(wins) / n * 100, 1)
        loss_rate = round(len(losses) / n * 100, 1)
        avg_pnl = round(sum(pnls) / n, 2)
        total_pnl = round(sum(pnls), 2)
        avg_rr = round(sum(rrs) / n, 2)
        profit_factor = _pf(pnls)

        # hold time — skip signals without closed_at
        hold_times = [
            (s.closed_at - s.created_at).total_seconds() / 60
            for s in closed
            if s.created_at and s.closed_at
        ]
        avg_hold_min = round(sum(hold_times) / len(hold_times), 0) if hold_times else None

        # ── LONG / SHORT ───────────────────────────────────────────────
        long_sigs = [s for s in closed if s.side == "LONG"]
        short_sigs = [s for s in closed if s.side == "SHORT"]

        # ── symbol leaderboard ─────────────────────────────────────────
        sym_map: dict = _dd(list)
        for s in closed:
            sym_map[s.symbol].append(s)

        leaderboard_rows = []
        for sym, sigs in sym_map.items():
            sw = [s for s in sigs if s.status in WIN_ST]
            sl = [s for s in sigs if s.status == "SL"]
            sp = [float(s.pnl_pct or 0) for s in sigs]
            sr = [float(s.risk_reward or 0) for s in sigs]
            nn = max(1, len(sigs))
            ls = [s for s in sigs if s.side == "LONG"]
            ss = [s for s in sigs if s.side == "SHORT"]
            lw = [s for s in ls if s.status in WIN_ST]
            shw = [s for s in ss if s.status in WIN_ST]
            leaderboard_rows.append(
                {
                    "symbol": sym,
                    "total": len(sigs),
                    "wins": len(sw),
                    "losses": len(sl),
                    "win_rate": round(len(sw) / nn * 100, 1),
                    "avg_pnl": round(sum(sp) / nn, 2),
                    "total_pnl": round(sum(sp), 2),
                    "avg_rr": round(sum(sr) / nn, 2),
                    "long": {
                        "total": len(ls),
                        "wins": len(lw),
                        "avg_pnl": round(
                            sum(float(s.pnl_pct or 0) for s in ls) / max(1, len(ls)), 2
                        ),
                    },
                    "short": {
                        "total": len(ss),
                        "wins": len(shw),
                        "avg_pnl": round(
                            sum(float(s.pnl_pct or 0) for s in ss) / max(1, len(ss)), 2
                        ),
                    },
                }
            )

        leaderboard_rows.sort(key=lambda x: x["total_pnl"], reverse=True)
        best5 = sorted(leaderboard_rows, key=lambda x: x["avg_pnl"], reverse=True)[:5]
        worst5 = sorted(leaderboard_rows, key=lambda x: x["avg_pnl"])[:5]

        # ── monthly breakdown ──────────────────────────────────────────
        mo_map: dict = _dd(list)
        for s in closed:
            if s.created_at:
                mo_map[s.created_at.strftime("%Y-%m")].append(s)

        monthly_rows = []
        for month, msigs in sorted(mo_map.items()):
            mw = [s for s in msigs if s.status in WIN_ST]
            ml = [s for s in msigs if s.status == "SL"]
            mp = [float(s.pnl_pct or 0) for s in msigs]
            mn = max(1, len(msigs))
            monthly_rows.append(
                {
                    "month": month,
                    "signals": len(msigs),
                    "wins": len(mw),
                    "losses": len(ml),
                    "win_rate": round(len(mw) / mn * 100, 1),
                    "total_pnl": round(sum(mp), 2),
                    "profit_factor": _pf(mp),
                }
            )

        return JSONResponse(
            {
                # ── primary schema (Sprint 4) ──────────────────────────────
                "total_signals": total_closed + open_count,
                "closed_signals": total_closed,
                "open_signals": open_count,
                "win_rate": win_rate,
                "loss_rate": loss_rate,
                "avg_pnl": avg_pnl,
                "total_pnl": total_pnl,
                "avg_rr": avg_rr,
                "profit_factor": profit_factor,  # null when no losses
                "avg_hold_time_minutes": avg_hold_min,
                "long": _side_stat(long_sigs),
                "short": _side_stat(short_sigs),
                "best_symbols": [
                    {"symbol": x["symbol"], "avg": x["avg_pnl"], "count": x["total"]} for x in best5
                ],
                "worst_symbols": [
                    {"symbol": x["symbol"], "avg": x["avg_pnl"], "count": x["total"]}
                    for x in worst5
                ],
                "symbol_leaderboard": leaderboard_rows,
                "monthly": monthly_rows,
                # ── backward-compat aliases ────────────────────────────────
                "total_closed": total_closed,
                "wins": len(wins),
                "losses": len(losses),
                "avg_hold_min": avg_hold_min,
                "leaderboard": [
                    {"symbol": x["symbol"], "avg": x["avg_pnl"], "count": x["total"]}
                    for x in leaderboard_rows[:10]
                ],
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/winrate-analysis")
async def api_public_winrate_analysis():
    """
    Sprint 16B — Auto winrate analyzer.
    Returns win rates broken down by side, confidence, RR, timeframe,
    funding classification, and OI direction.
    """
    try:
        from app.analytics.winrate import compute_winrate_analysis

        result = await compute_winrate_analysis()
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/backtest/run")
async def api_backtest_run(
    symbol: str = "BTCUSDT",
    start: str = "",
    end: str = "",
    strategy: str = "V3.2",
):
    """
    Real historical candle-replay backtest.

    Query params:
        symbol   — Binance USDT-M pair (e.g. BTCUSDT)
        start    — ISO date YYYY-MM-DD (UTC, inclusive)
        end      — ISO date YYYY-MM-DD (UTC, inclusive)
        strategy — strategy label (currently V3.2 only)
    """
    from app.backtesting.historical import HistoricalBacktestEngine

    # ── input sanitisation ────────────────────────────────────────
    symbol = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()[:20]
    if not symbol:
        return JSONResponse({"error": "symbol is required"}, status_code=400)

    # Default date range: last 90 days
    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    _today = datetime.now(_tz.utc).date()
    if not end:
        end = str(_today - timedelta(days=1))
    if not start:
        start = str(_today - timedelta(days=91))

    # Validate dates
    _date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if not _date_re.match(start) or not _date_re.match(end):
        return JSONResponse({"error": "dates must be YYYY-MM-DD"}, status_code=400)

    try:
        engine = HistoricalBacktestEngine()
        result = await engine.run(symbol, start, end, strategy)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    if result.error:
        return JSONResponse({"error": result.error}, status_code=400)

    return JSONResponse(result.to_dict())


@router.get("/api/backtest")
async def api_backtest():
    """
    Sprint 7 canonical backtest endpoint.
    strategy = MTF_SMC_STRICT · timeframe IN (15m/1h/4h/1d) · closed only.
    """
    try:
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
                )
                .order_by(Signal.created_at)
            )
            signals = list(res.scalars().all())
        return JSONResponse(_compute_backtest(signals))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/backtest")
async def api_public_backtest():
    """Backward-compat alias → delegates to _compute_backtest()."""
    try:
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
                )
                .order_by(Signal.created_at)
            )
            signals = list(res.scalars().all())
        return JSONResponse(_compute_backtest(signals))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/performance-center")
async def api_performance_center():
    """Multi-period signal analytics: 24h/7D/30D, bands, pairs, distribution."""
    cached = _cache_get("perf_center")
    if cached is not None:
        return JSONResponse(cached)

    from collections import defaultdict as _dd

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    WIN_ST = ("TP1", "TP2", "TP3")

    try:
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.created_at >= cutoff_30d,
                )
                .order_by(Signal.created_at)
            )
            all_sigs = list(res.scalars().all())

        def _period(sigs):
            closed = [s for s in sigs if s.status in ("TP1", "TP2", "TP3", "SL", "EXPIRED")]
            open_s = [s for s in sigs if s.status == "OPEN"]
            tp1 = [s for s in sigs if s.status == "TP1"]
            tp2 = [s for s in sigs if s.status == "TP2"]
            sl = [s for s in sigs if s.status == "SL"]
            exp = [s for s in sigs if s.status == "EXPIRED"]
            wins = [s for s in closed if s.status in WIN_ST]
            nc = max(1, len(closed))
            wr = round(len(wins) / nc * 100, 1) if len(closed) >= 5 else None
            return {
                "total": len(sigs),
                "closed": len(closed),
                "tp1": len(tp1),
                "tp2": len(tp2),
                "sl": len(sl),
                "expired": len(exp),
                "open": len(open_s),
                "closed_winrate": wr,
                "sample_ok": len(closed) >= 30,
            }

        sigs_24h = [s for s in all_sigs if s.created_at and s.created_at >= cutoff_24h]
        sigs_7d = [s for s in all_sigs if s.created_at and s.created_at >= cutoff_7d]
        sigs_30d = all_sigs

        closed_30d = [s for s in sigs_30d if s.status in ("TP1", "TP2", "TP3", "SL")]

        # Long vs Short
        def _side(sigs):
            wins = [s for s in sigs if s.status in WIN_ST]
            n = max(1, len(sigs))
            return {"total": len(sigs), "winrate": round(len(wins) / n * 100, 1)}

        long_30d = [s for s in closed_30d if s.side == "LONG"]
        short_30d = [s for s in closed_30d if s.side == "SHORT"]

        # Best / Worst pairs
        sym_map: dict = _dd(list)
        for s in closed_30d:
            sym_map[s.symbol].append(s)

        pair_rows = []
        for sym, sigs in sym_map.items():
            wins = [s for s in sigs if s.status in WIN_ST]
            rrs = [float(s.risk_reward or 0) for s in sigs]
            n = len(sigs)
            pair_rows.append(
                {
                    "symbol": sym,
                    "total": n,
                    "winrate": round(len(wins) / n * 100, 1),
                    "avg_rr": round(sum(rrs) / max(1, n), 2),
                }
            )

        best_pairs = sorted(pair_rows, key=lambda x: x["winrate"], reverse=True)[:5]
        worst_pairs = sorted(pair_rows, key=lambda x: x["winrate"])[:5]

        # Confidence bands (30D all statuses)
        def _band(lo, hi):
            bsigs = [s for s in sigs_30d if lo <= float(s.confidence or 0) < hi]
            wins = [s for s in bsigs if s.status in WIN_ST]
            losses = [s for s in bsigs if s.status == "SL"]
            n = len(bsigs)
            return {
                "signals": n,
                "wins": len(wins),
                "losses": len(losses),
                "winrate": round(len(wins) / n * 100, 1) if n >= 3 else None,
            }

        # Status distribution (30D)
        status_dist = {
            k: sum(1 for s in sigs_30d if s.status == k)
            for k in ("OPEN", "TP1", "TP2", "SL", "EXPIRED")
        }

        total_closed_30d = len(closed_30d)

        result = {
            "sample_size": total_closed_30d,
            "data_collecting": total_closed_30d < 30,
            "period_24h": _period(sigs_24h),
            "period_7d": _period(sigs_7d),
            "period_30d": _period(sigs_30d),
            "long_vs_short": {"long": _side(long_30d), "short": _side(short_30d)},
            "best_pairs": best_pairs,
            "worst_pairs": worst_pairs,
            "confidence_bands": {
                "75_80": _band(75, 80),
                "80_85": _band(80, 85),
                "85_90": _band(85, 90),
                "90_plus": _band(90, 200),
            },
            "status_distribution": status_dist,
            "updated_at": now.isoformat(),
        }
        _cache_set("perf_center", result)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/market-radar")
async def api_market_radar():
    """Daily market intelligence: bias, risk, setups, sentiment."""
    cached = _cache_get("market_radar")
    if cached is not None:
        return JSONResponse(cached)

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_2h = now - timedelta(hours=2)

    try:
        async with SessionLocal() as session:
            sig_res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.created_at >= cutoff_24h,
                )
                .order_by(desc(Signal.created_at))
            )
            recent_sigs = list(sig_res.scalars().all())

            fund_subq = (
                select(
                    FundingRateSnapshot.symbol,
                    _sqlfunc.max(FundingRateSnapshot.created_at).label("latest"),
                )
                .where(FundingRateSnapshot.created_at >= cutoff_2h)
                .group_by(FundingRateSnapshot.symbol)
                .subquery()
            )
            fund_res = await session.execute(
                select(FundingRateSnapshot).join(
                    fund_subq,
                    (FundingRateSnapshot.symbol == fund_subq.c.symbol)
                    & (FundingRateSnapshot.created_at == fund_subq.c.latest),
                )
            )
            funding_snaps = list(fund_res.scalars().all())

        def _bias(sigs):
            if not sigs:
                return "NEUTRAL"
            longs = sum(1 for s in sigs if s.side == "LONG")
            r = longs / len(sigs)
            return "BULLISH" if r >= 0.65 else ("BEARISH" if r <= 0.35 else "NEUTRAL")

        btc_sigs = [s for s in recent_sigs if s.symbol == "BTCUSDT"]
        eth_sigs = [s for s in recent_sigs if s.symbol == "ETHUSDT"]
        other_sigs = [s for s in recent_sigs if s.symbol not in ("BTCUSDT", "ETHUSDT")]

        extreme_pos = sum(1 for s in funding_snaps if s.classification == "extreme_positive")
        extreme_neg = sum(1 for s in funding_snaps if s.classification == "extreme_negative")
        total_fund = max(1, len(funding_snaps))
        er = (extreme_pos + extreme_neg) / total_fund
        market_risk = "HIGH" if er >= 0.30 else ("MEDIUM" if er >= 0.12 else "LOW")

        strongest = [
            {
                "symbol": s.symbol,
                "side": s.side,
                "confidence": round(float(s.confidence or 0), 1),
                "rr": round(float(s.risk_reward or 0), 2),
                "status": s.status,
                "tf": s.timeframe,
            }
            for s in sorted(recent_sigs, key=lambda x: float(x.confidence or 0), reverse=True)[:10]
        ]

        total_sigs = len(recent_sigs)
        long_count = sum(1 for s in recent_sigs if s.side == "LONG")
        short_count = total_sigs - long_count
        dir_score = (long_count / max(1, total_sigs)) * 60
        fund_adj = -((extreme_pos - extreme_neg) / total_fund) * 20
        sentiment_score = max(0, min(100, round(dir_score + 20 + fund_adj)))
        sentiment_label = (
            "GREED" if sentiment_score >= 60 else ("FEAR" if sentiment_score <= 40 else "NEUTRAL")
        )

        _SECTOR_MAP = {
            "Layer 1": {
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "AVAXUSDT",
                "DOTUSDT",
                "NEARUSDT",
                "ADAUSDT",
                "TRXUSDT",
            },
            "DeFi": {
                "UNIUSDT",
                "AAVEUSDT",
                "COMPUSDT",
                "CRVUSDT",
                "MKRUSDT",
                "DYDXUSDT",
                "SNXUSDT",
            },
            "Layer 2": {"MATICUSDT", "OPUSDT", "ARBUSDT", "STRKUSDT"},
            "Meme": {"DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "FLOKIUSDT", "BONKUSDT", "WIFUSDT"},
            "AI/Data": {"FETUSDT", "WLDUSDT", "TAOUSDT", "RENDERUSDT", "OCEANUSDT"},
            "Gaming": {"AXSUSDT", "SANDUSDT", "MANAUSDT", "GALAUSDT", "IMXUSDT"},
        }
        sector_stats = []
        for sector, syms in _SECTOR_MAP.items():
            ssigs = [s for s in recent_sigs if s.symbol in syms]
            if ssigs:
                longs = sum(1 for s in ssigs if s.side == "LONG")
                r = longs / len(ssigs)
                sector_stats.append(
                    {
                        "sector": sector,
                        "signals": len(ssigs),
                        "bias": "BULLISH" if r >= 0.6 else ("BEARISH" if r <= 0.4 else "NEUTRAL"),
                    }
                )
        if not sector_stats:
            sector_stats = [{"sector": "Sector data collecting", "signals": 0, "bias": "NEUTRAL"}]

        result = {
            "market_bias": {
                "btc": _bias(btc_sigs),
                "eth": _bias(eth_sigs),
                "altcoin": _bias(other_sigs),
            },
            "market_risk": market_risk,
            "strongest_setups": strongest,
            "sector_radar": sector_stats,
            "futures_sentiment": {"score": sentiment_score, "label": sentiment_label},
            "signals_24h": total_sigs,
            "long_count_24h": long_count,
            "short_count_24h": short_count,
            "updated_at": now.isoformat(),
        }
        _cache_set("market_radar", result)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/setup-library")
async def api_setup_library():
    """Educational setup library — trading concept explanations only."""
    return JSONResponse({"setups": _SETUP_LIBRARY, "count": len(_SETUP_LIBRARY)})


@router.get("/api/public/market-regime")
async def api_public_market_regime():
    """Current market regime classification and supporting metrics."""
    try:
        from app.market_data.market_regime import get_market_regime

        regime = await get_market_regime()
        if regime is None:
            return JSONResponse(
                {
                    "error": "market regime not yet calculated — try again after the first scan cycle"
                },
                status_code=503,
            )
        return JSONResponse(
            {
                "market_regime": regime.market_regime,
                "regime_score": regime.regime_score,
                "breadth": regime.breadth_ema200,
                "breadth_ema50": regime.breadth_ema50,
                "btc_trend": regime.btc_trend,
                "eth_trend": regime.eth_trend,
                "atr_percentile": regime.atr_percentile,
                "calculated_at": regime.calculated_at,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/regime-adaptive-thresholds")
async def api_public_regime_adaptive_thresholds():
    """Current base vs regime-adapted RR / SL-distance / confidence thresholds."""
    try:
        from app.config import settings
        from app.market_data.market_regime import get_market_regime
        from app.risk.regime_adaptive_gate import get_effective_thresholds

        regime = await get_market_regime()
        thr = get_effective_thresholds(
            base_min_rr=settings.min_rr,
            base_max_sl_distance_percent=settings.max_sl_distance_percent,
            base_min_confidence=settings.min_confidence,
            market_regime=regime.market_regime if regime else None,
        )
        return JSONResponse(
            {
                "enabled": thr.enabled,
                "market_regime": thr.market_regime,
                "base": {
                    "min_rr": thr.base_min_rr,
                    "max_sl_distance_percent": thr.base_max_sl_distance_percent,
                    "min_confidence": thr.base_min_confidence,
                },
                "effective": {
                    "min_rr": thr.effective_min_rr,
                    "max_sl_distance_percent": thr.effective_max_sl_distance_percent,
                    "min_confidence": thr.effective_min_confidence,
                },
                "confidence_delta": thr.confidence_delta,
                "reason": thr.reason,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/short-protection")
async def api_public_short_protection():
    """Short protection filter statistics — rejection counts and top reasons."""
    try:
        from app.scanner.short_protection import get_short_protection_stats

        stats = await get_short_protection_stats()
        return JSONResponse(stats)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
