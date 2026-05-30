from __future__ import annotations

import html as html_lib
import os
import re
import time
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from sqlalchemy import func as _sqlfunc, select, desc

from app.config import settings
from app.database.session import SessionLocal
from app.database.models import Signal, AffiliateClick, PaperPosition
from app.database.repo import get_active_signals_summary, ACTIVE_STATUSES
from app.market_data import universe
from app.market_data.ws_engine import ws_health


# ── auth ──────────────────────────────────────────────────────────

def _admin_user() -> str:
    return os.getenv("DASHBOARD_USER", "admin")


def _admin_password() -> str:
    """Admin password must be supplied via .env in public deployments."""
    return os.getenv("DASHBOARD_PASSWORD", "").strip()


def _admin_auth_configured() -> bool:
    return bool(_admin_user() and _admin_password())


def _esc(value: str) -> str:
    return html_lib.escape(str(value or ""), quote=True)


def _safe_url(value: str) -> str:
    """Allow only http(s) URLs in public href attributes."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return _esc(raw)


def _safe_wallet(value: str, max_len: int = 140) -> str:
    """Conservative wallet/address renderer for public donate section."""
    raw = str(value or "").strip()[:max_len]
    # Keep wallet display resilient; still escape everything before rendering.
    return _esc(raw)


def _js_single_quote(value: str) -> str:
    """Escape value for use inside a single-quoted inline JS string."""
    raw = str(value or "")
    raw = raw.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "").replace("\r", "")
    return _esc(raw)

def _is_logged_in(request: Request) -> bool:
    return request.cookies.get("alpha_radar_auth") == "ok"


def _login_page(error: str = "") -> HTMLResponse:
    if not _admin_auth_configured():
        error = "Admin login is disabled until DASHBOARD_USER and DASHBOARD_PASSWORD are set in .env"
    err = f"<div class='err'>{_esc(error)}</div>" if error else ""
    return HTMLResponse(_LOGIN_HTML.replace("__ERR__", err))


# ── app ───────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    print("dashboard starting")
    yield


app = FastAPI(title="ALPHA RADAR SIGNALS", lifespan=_lifespan)
_boot_time = time.time()

# Production filter: only V3 MTF signals appear on all public-facing queries.
# Legacy 5m / old-engine signals live in archive_signals after migration.
_MTF_TIMEFRAMES = ["15m", "1h", "4h", "1d"]
_MTF_STRATEGY   = "MTF_SMC_STRICT"


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response


app.add_middleware(_SecurityHeaders)


# ── stats helper ──────────────────────────────────────────────────

async def _get_stats() -> dict:
    now = datetime.now(timezone.utc)
    prod_start_raw = os.getenv("PRODUCTION_START_UTC", "").strip()
    try:
        start7 = (
            datetime.strptime(prod_start_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if prod_start_raw else now - timedelta(days=7)
        )
    except Exception:
        start7 = now - timedelta(days=7)

    async with SessionLocal() as session:
        week_res = await session.execute(
            select(Signal)
            .where(
                Signal.created_at >= start7,
                Signal.strategy == _MTF_STRATEGY,
                Signal.timeframe.in_(_MTF_TIMEFRAMES),
            )
            .order_by(desc(Signal.created_at)).limit(500)
        )
        week = week_res.scalars().all()
        recent_res = await session.execute(
            select(Signal)
            .where(
                Signal.strategy == _MTF_STRATEGY,
                Signal.timeframe.in_(_MTF_TIMEFRAMES),
            )
            .order_by(desc(Signal.created_at)).limit(20)
        )
        recent = recent_res.scalars().all()

    closed = [s for s in week if s.status in ("TP1", "TP2", "TP3", "SL")]
    open_sigs = [s for s in week if s.status == "OPEN"]
    wins = [s for s in closed if s.status in ("TP1", "TP2", "TP3")]
    losses = [s for s in closed if s.status == "SL"]
    winrate = len(wins) / max(1, len(wins) + len(losses)) * 100
    avg_pnl = sum(float(s.pnl_pct or 0) for s in closed) / max(1, len(closed))

    sym_map: dict = {}
    for s in closed:
        sym_map.setdefault(s.symbol, []).append(float(s.pnl_pct or 0))
    leaderboard = sorted(
        [{"symbol": k, "avg": round(sum(v) / len(v), 2), "count": len(v)} for k, v in sym_map.items()],
        key=lambda x: x["avg"], reverse=True,
    )[:10]

    def _row(s):
        return {
            "id": s.id,
            "time": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
            "symbol": s.symbol, "side": s.side, "tf": s.timeframe,
            "conf": round(float(s.confidence or 0), 1),
            "rr": round(float(s.risk_reward or 0), 2),
            "status": s.status,
            "pnl": round(float(s.pnl_pct or 0), 2),
            "entry_low": round(float(s.entry_low or 0), 6),
            "tp1": round(float(s.tp1 or 0), 6),
            "sl": round(float(s.stop_loss or 0), 6),
        }

    return {
        "winrate": round(winrate, 1),
        "signals7d": len(week),
        "open_signals": len(open_sigs),
        "closed_signals": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "avgpnl": round(avg_pnl, 2),
        "universe": len(universe.symbols),
        "leaderboard": leaderboard,
        "recent": [_row(s) for s in recent],
        "open": [_row(s) for s in open_sigs[:15]],
        "closed_recent": [_row(s) for s in closed[:15]],
    }


# ── public API (no auth) ──────────────────────────────────────────

@app.get("/api/public/stats")
async def api_public_stats():
    try:
        return JSONResponse(await _get_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/public/signals")
async def api_public_signals(limit: int = 50):
    limit = min(max(1, limit), 200)
    try:
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
                .order_by(desc(Signal.created_at)).limit(limit)
            )
            rows = res.scalars().all()
        return JSONResponse([{
            "id": s.id,
            "symbol": s.symbol,
            "side": s.side,
            "timeframe": s.timeframe,
            "confidence": round(float(s.confidence or 0), 1),
            "risk_reward": round(float(s.risk_reward or 0), 2),
            "status": s.status,
            "pnl_pct": round(float(s.pnl_pct or 0), 2),
            "entry_low": round(float(s.entry_low or 0), 6),
            "tp1": round(float(s.tp1 or 0), 6),
            "sl": round(float(s.stop_loss or 0), 6),
            "created_at": s.created_at.isoformat() if s.created_at else None,
        } for s in rows])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/public/performance")
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
                select(_sqlfunc.count(Signal.id))
                .where(
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
            n  = max(1, len(sigs))
            return {
                "total":    len(sigs),
                "wins":     len(sw),
                "losses":   len(sl),
                "win_rate": round(len(sw) / n * 100, 1),
                "avg_pnl":  round(sum(sp) / n, 2),
                "avg_rr":   round(sum(sr) / n, 2),
            }

        # ── overall metrics ────────────────────────────────────────────
        total_closed = len(closed)
        wins   = [s for s in closed if s.status in WIN_ST]
        losses = [s for s in closed if s.status == "SL"]
        pnls   = [float(s.pnl_pct or 0)    for s in closed]
        rrs    = [float(s.risk_reward or 0) for s in closed]
        n      = max(1, total_closed)

        win_rate  = round(len(wins)   / n * 100, 1)
        loss_rate = round(len(losses) / n * 100, 1)
        avg_pnl   = round(sum(pnls) / n, 2)
        total_pnl = round(sum(pnls),     2)
        avg_rr    = round(sum(rrs)  / n, 2)
        profit_factor = _pf(pnls)

        # hold time — skip signals without closed_at
        hold_times = [
            (s.closed_at - s.created_at).total_seconds() / 60
            for s in closed if s.created_at and s.closed_at
        ]
        avg_hold_min = round(sum(hold_times) / len(hold_times), 0) if hold_times else None

        # ── LONG / SHORT ───────────────────────────────────────────────
        long_sigs  = [s for s in closed if s.side == "LONG"]
        short_sigs = [s for s in closed if s.side == "SHORT"]

        # ── symbol leaderboard ─────────────────────────────────────────
        sym_map: dict = _dd(list)
        for s in closed:
            sym_map[s.symbol].append(s)

        leaderboard_rows = []
        for sym, sigs in sym_map.items():
            sw = [s for s in sigs if s.status in WIN_ST]
            sl = [s for s in sigs if s.status == "SL"]
            sp = [float(s.pnl_pct or 0)    for s in sigs]
            sr = [float(s.risk_reward or 0) for s in sigs]
            nn = max(1, len(sigs))
            ls = [s for s in sigs if s.side == "LONG"]
            ss = [s for s in sigs if s.side == "SHORT"]
            lw = [s for s in ls if s.status in WIN_ST]
            shw = [s for s in ss if s.status in WIN_ST]
            leaderboard_rows.append({
                "symbol":    sym,
                "total":     len(sigs),
                "wins":      len(sw),
                "losses":    len(sl),
                "win_rate":  round(len(sw) / nn * 100, 1),
                "avg_pnl":   round(sum(sp) / nn, 2),
                "total_pnl": round(sum(sp), 2),
                "avg_rr":    round(sum(sr) / nn, 2),
                "long": {
                    "total":   len(ls),
                    "wins":    len(lw),
                    "avg_pnl": round(sum(float(s.pnl_pct or 0) for s in ls) / max(1, len(ls)), 2),
                },
                "short": {
                    "total":   len(ss),
                    "wins":    len(shw),
                    "avg_pnl": round(sum(float(s.pnl_pct or 0) for s in ss) / max(1, len(ss)), 2),
                },
            })

        leaderboard_rows.sort(key=lambda x: x["total_pnl"], reverse=True)
        best5  = sorted(leaderboard_rows, key=lambda x: x["avg_pnl"], reverse=True)[:5]
        worst5 = sorted(leaderboard_rows, key=lambda x: x["avg_pnl"])[:5]

        # ── monthly breakdown ──────────────────────────────────────────
        mo_map: dict = _dd(list)
        for s in closed:
            if s.created_at:
                mo_map[s.created_at.strftime("%Y-%m")].append(s)

        monthly_rows = []
        for month, msigs in sorted(mo_map.items()):
            mw  = [s for s in msigs if s.status in WIN_ST]
            ml  = [s for s in msigs if s.status == "SL"]
            mp  = [float(s.pnl_pct or 0) for s in msigs]
            mn  = max(1, len(msigs))
            monthly_rows.append({
                "month":         month,
                "signals":       len(msigs),
                "wins":          len(mw),
                "losses":        len(ml),
                "win_rate":      round(len(mw) / mn * 100, 1),
                "total_pnl":     round(sum(mp), 2),
                "profit_factor": _pf(mp),
            })

        return JSONResponse({
            # ── primary schema (Sprint 4) ──────────────────────────────
            "total_signals":        total_closed + open_count,
            "closed_signals":       total_closed,
            "open_signals":         open_count,
            "win_rate":             win_rate,
            "loss_rate":            loss_rate,
            "avg_pnl":              avg_pnl,
            "total_pnl":            total_pnl,
            "avg_rr":               avg_rr,
            "profit_factor":        profit_factor,   # null when no losses
            "avg_hold_time_minutes":avg_hold_min,
            "long":                 _side_stat(long_sigs),
            "short":                _side_stat(short_sigs),
            "best_symbols":         [{"symbol": x["symbol"], "avg": x["avg_pnl"], "count": x["total"]} for x in best5],
            "worst_symbols":        [{"symbol": x["symbol"], "avg": x["avg_pnl"], "count": x["total"]} for x in worst5],
            "symbol_leaderboard":   leaderboard_rows,
            "monthly":              monthly_rows,
            # ── backward-compat aliases ────────────────────────────────
            "total_closed":         total_closed,
            "wins":                 len(wins),
            "losses":               len(losses),
            "avg_hold_min":         avg_hold_min,
            "leaderboard": [{"symbol": x["symbol"], "avg": x["avg_pnl"], "count": x["total"]} for x in leaderboard_rows[:10]],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/paper")
async def api_paper():
    """Paper trading — stats + latest positions (Sprint 6 primary endpoint)."""
    try:
        from app.paper.trading import get_portfolio_stats, get_positions
        stats = await get_portfolio_stats()
        open_pos   = await get_positions(status="open",   limit=50)
        closed_pos = await get_positions(status="closed", limit=50)
        return JSONResponse({**stats, "open": open_pos, "closed": closed_pos})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/paper/positions")
async def api_paper_positions(status: str = "", limit: int = 100):
    """Paper trading — positions list (filter: open / closed / all)."""
    limit = min(max(1, limit), 500)
    try:
        from app.paper.trading import get_positions
        rows = await get_positions(
            status=status.lower() if status else None,
            limit=limit,
        )
        return JSONResponse({"positions": rows, "count": len(rows)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/paper/stats")
async def api_paper_stats():
    """Paper trading — portfolio statistics only."""
    try:
        from app.paper.trading import get_portfolio_stats
        return JSONResponse(await get_portfolio_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/public/prices")
async def api_public_prices():
    return JSONResponse(ws_health())


@app.get("/api/public/signal/{signal_id}")
async def api_public_signal(signal_id: int):
    try:
        async with SessionLocal() as session:
            sig = await session.get(Signal, signal_id)
        if sig is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        reasons_list = [r.strip() for r in (sig.reasons or "").split("|") if r.strip()]
        return JSONResponse({
            "id": sig.id,
            "symbol": sig.symbol,
            "side": sig.side,
            "timeframe": sig.timeframe,
            "confidence": round(float(sig.confidence or 0), 1),
            "risk_reward": round(float(sig.risk_reward or 0), 2),
            "risk_level": sig.risk_level or "",
            "strategy": sig.strategy or "",
            "status": sig.status,
            "pnl_pct": round(float(sig.pnl_pct or 0), 2),
            "entry_low": round(float(sig.entry_low or 0), 6),
            "entry_high": round(float(sig.entry_high or 0), 6),
            "tp1": round(float(sig.tp1 or 0), 6),
            "tp2": round(float(sig.tp2 or 0), 6),
            "tp3": round(float(sig.tp3 or 0), 6),
            "stop_loss": round(float(sig.stop_loss or 0), 6),
            "trend_score": sig.trend_score,
            "structure_score": sig.structure_score,
            "setup_score": sig.setup_score,
            "entry_score": sig.entry_score,
            "reasons": reasons_list,
            "created_at": sig.created_at.isoformat() if sig.created_at else None,
            "closed_at": sig.closed_at.isoformat() if sig.closed_at else None,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/public/paper")
async def api_public_paper():
    """Paper trading portfolio derived from all MTF signals (virtual 10 000 USDT)."""
    INITIAL_BALANCE = 10_000.0
    RISK_PCT = 0.01  # 1% per trade

    try:
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
                .order_by(Signal.created_at)
                .limit(500)
            )
            signals = res.scalars().all()

        open_pos = [s for s in signals if s.status == "OPEN"]
        closed_pos = [s for s in signals if s.status in ("TP1", "TP2", "TP3", "SL")]

        running_balance = INITIAL_BALANCE
        closed_rows = []
        for s in closed_pos:
            size = running_balance * RISK_PCT
            pnl_usdt = round(size * float(s.pnl_pct or 0) / 100, 2)
            running_balance += pnl_usdt
            closed_rows.append({
                "id": s.id,
                "symbol": s.symbol,
                "side": s.side,
                "entry": round(float(s.entry_low or 0), 6),
                "sl": round(float(s.stop_loss or 0), 6),
                "tp1": round(float(s.tp1 or 0), 6),
                "status": s.status,
                "pnl_pct": round(float(s.pnl_pct or 0), 2),
                "pnl_usdt": pnl_usdt,
                "opened": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
            })

        open_rows = [{
            "id": s.id,
            "symbol": s.symbol,
            "side": s.side,
            "entry": round(float(s.entry_low or 0), 6),
            "sl": round(float(s.stop_loss or 0), 6),
            "tp1": round(float(s.tp1 or 0), 6),
            "conf": round(float(s.confidence or 0), 1),
            "rr": round(float(s.risk_reward or 0), 2),
            "opened": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
        } for s in open_pos]

        wins = [r for r in closed_rows if r["status"] in ("TP1", "TP2", "TP3")]
        total_pnl_usdt = round(sum(r["pnl_usdt"] for r in closed_rows), 2)
        win_rate = round(len(wins) / max(1, len(closed_rows)) * 100, 1)

        return JSONResponse({
            "initial_balance": INITIAL_BALANCE,
            "current_balance": round(running_balance, 2),
            "total_pnl_usdt": total_pnl_usdt,
            "total_pnl_pct": round((running_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
            "win_rate": win_rate,
            "total_trades": len(closed_rows),
            "open_count": len(open_rows),
            "open": open_rows,
            "closed": list(reversed(closed_rows))[:50],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _compute_backtest(signals: list) -> dict:
    """
    Core backtest computation — pure function, no DB calls.
    Called by both GET /api/backtest and GET /api/public/backtest.
    strategy = MTF_SMC_STRICT · timeframe IN (15m/1h/4h/1d) · closed only.
    """
    import math as _m
    from collections import defaultdict as _dd

    _EMPTY = {
        "total": 0, "wins": 0, "losses": 0,
        "win_rate": 0.0, "profit_factor": 0.0,
        "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0, "avg_rr": 0.0, "avg_pnl": 0.0,
        "total_pnl": 0.0, "rr_distribution": [],
        "equity_curve": [0.0], "monthly": [],
    }
    if not signals:
        return _EMPTY

    WIN_ST = ("TP1", "TP2", "TP3")
    wins   = [s for s in signals if s.status in WIN_ST]
    losses = [s for s in signals if s.status == "SL"]
    pnls   = [float(s.pnl_pct   or 0) for s in signals]
    rrs    = [float(s.risk_reward or 0) for s in signals]
    n      = len(signals)

    # ── core metrics ──────────────────────────────────────────────
    gross_win  = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    mean_pnl = sum(pnls) / max(1, n)
    sharpe   = 0.0
    if n > 1:
        var = sum((p - mean_pnl) ** 2 for p in pnls) / n
        sharpe = round(mean_pnl / max(0.001, _m.sqrt(var)), 2)

    # ── equity curve (cumulative PnL %) + max drawdown ───────────
    cum = peak = max_dd = 0.0
    equity_curve = [0.0]
    for p in pnls:
        cum   = round(cum + p, 2)
        equity_curve.append(cum)
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # ── RR distribution ───────────────────────────────────────────
    rr_buckets: dict = {}
    for rr in rrs:
        b = f"{_m.floor(rr * 2) / 2:.1f}"
        rr_buckets[b] = rr_buckets.get(b, 0) + 1
    rr_dist = sorted(
        [{"rr": k, "count": v} for k, v in rr_buckets.items()],
        key=lambda x: float(x["rr"])
    )

    # ── monthly breakdown ─────────────────────────────────────────
    mo_map: dict = _dd(list)
    for s in signals:
        if s.created_at:
            mo_map[s.created_at.strftime("%Y-%m")].append(s)

    monthly_rows = []
    for month, msigs in sorted(mo_map.items()):
        mw   = [s for s in msigs if s.status in WIN_ST]
        ml   = [s for s in msigs if s.status == "SL"]
        mp   = [float(s.pnl_pct or 0) for s in msigs]
        mn   = max(1, len(msigs))
        mgw  = sum(p for p in mp if p > 0)
        mgl  = abs(sum(p for p in mp if p < 0))
        m_pf = round(mgw / mgl, 2) if mgl > 0 else None
        monthly_rows.append({
            "month":         month,
            "signals":       len(msigs),
            "wins":          len(mw),
            "losses":        len(ml),
            "win_rate":      round(len(mw) / mn * 100, 1),
            "total_pnl":     round(sum(mp), 2),
            "profit_factor": m_pf,
        })

    return {
        "total":            n,
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         round(len(wins) / n * 100, 1),
        "profit_factor":    profit_factor,
        "max_drawdown":     round(max_dd, 2),
        "max_drawdown_pct": round(max_dd, 2),   # backward-compat alias
        "sharpe_ratio":     sharpe,
        "avg_rr":           round(sum(rrs) / max(1, n), 2),
        "avg_pnl":          round(mean_pnl, 2),
        "total_pnl":        round(sum(pnls), 2),
        "rr_distribution":  rr_dist,
        "equity_curve":     equity_curve[-61:],  # max 61 points (60 trades + start)
        "monthly":          monthly_rows,
    }


@app.get("/api/backtest/run")
async def api_backtest_run(
    symbol:   str = "BTCUSDT",
    start:    str = "",
    end:      str = "",
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
    import re
    symbol = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()[:20]
    if not symbol:
        return JSONResponse({"error": "symbol is required"}, status_code=400)

    # Default date range: last 90 days
    from datetime import datetime, timedelta, timezone as _tz
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


@app.get("/api/backtest")
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


@app.get("/api/public/backtest")
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


@app.get("/aff/{exchange}")
async def affiliate_redirect(exchange: str, request: Request):
    """Track affiliate click then redirect to the affiliate URL."""
    exchange = exchange.lower().strip()
    url_map = {
        "binance": settings.binance_affiliate_url,
        "bybit":   settings.bybit_affiliate_url,
        "okx":     settings.okx_affiliate_url,
        "bitget":  settings.bitget_affiliate_url,
    }
    dest = _safe_url(url_map.get(exchange, ""))
    if not dest:
        return RedirectResponse("/", status_code=302)

    # Record click
    try:
        async with SessionLocal() as session:
            click = AffiliateClick(
                exchange=exchange,
                referrer=request.headers.get("referer", "")[:256],
            )
            session.add(click)
            await session.commit()
    except Exception:
        pass

    return RedirectResponse(dest, status_code=302)


@app.get("/api/admin/affiliate-stats")
async def affiliate_stats(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from sqlalchemy import func as sqlfunc
        from sqlalchemy import text
        async with SessionLocal() as session:
            res = await session.execute(
                select(AffiliateClick.exchange, sqlfunc.count(AffiliateClick.id).label("clicks"))
                .group_by(AffiliateClick.exchange)
                .order_by(sqlfunc.count(AffiliateClick.id).desc())
            )
            rows = res.fetchall()
        return JSONResponse([{"exchange": r[0], "clicks": r[1]} for r in rows])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/admin/active-signals")
async def admin_active_signals(request: Request):
    """Return all currently active (OPEN/ACTIVE/PENDING) signals for duplicate monitoring."""
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        rows = await get_active_signals_summary()
        return JSONResponse({"active": rows, "count": len(rows)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/performance/rebuild")
async def api_performance_rebuild(request: Request):
    """
    Trigger a full performance rebuild from the admin dashboard.
    Requires admin login. Recomputes all 5 metrics and rebuilds
    daily_stats + weekly_stats using only MTF_SMC_STRICT signals.
    """
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from app.performance.rebuild import rebuild as _rebuild
        result = await _rebuild()
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── monitoring (no auth) ──────────────────────────────────────────

@app.get("/health", response_class=HTMLResponse)
async def health():
    return HTMLResponse(_health_page_html())


@app.get("/api/health")
async def api_health():
    """
    Sprint 5 Health Center API.
    Returns per-service status for: dashboard, database, redis, binance,
    telegram, scanner, worker, scheduler — plus activity metrics.
    Backward-compat fields (uptime_sec, components, config) are preserved.
    """
    now_ts  = datetime.now(timezone.utc)
    checked = now_ts.isoformat()
    uptime  = round(time.time() - _boot_time)

    def _svc(ok: bool, *, error: str | None = None,
              latency_ms: float | None = None, detail: str | None = None,
              **extra) -> dict:
        s: dict = {
            "ok":         ok,
            "status":     "ONLINE" if ok else "OFFLINE",
            "checked_at": checked,
            "latency_ms": latency_ms,
            "error":      error,
        }
        if detail:
            s["detail"] = detail
        s.update(extra)
        return s

    # ── Dashboard ─────────────────────────────────────────────────────
    svc_dashboard = _svc(True, detail=f"port {settings.dashboard_port}",
                         uptime_seconds=uptime)

    # ── Database ──────────────────────────────────────────────────────
    db_ok = False
    db_lat: float | None = None
    db_err: str | None = None
    try:
        t0 = time.monotonic()
        async with SessionLocal() as s:
            await s.execute(select(Signal).limit(1))
        db_lat = round((time.monotonic() - t0) * 1000, 1)
        db_ok = True
    except Exception as exc:
        db_err = str(exc)[:150]
    svc_database = _svc(db_ok, latency_ms=db_lat, error=db_err,
                        detail="PostgreSQL (asyncpg)")

    # ── Redis ─────────────────────────────────────────────────────────
    redis_ok = False
    redis_lat: float | None = None
    redis_err: str | None = None
    try:
        from app.market_data.cache import get_redis
        t0 = time.monotonic()
        r = await get_redis()
        await r.ping()
        redis_lat = round((time.monotonic() - t0) * 1000, 1)
        redis_ok = True
    except Exception as exc:
        redis_err = str(exc)[:150]
    svc_redis = _svc(redis_ok, latency_ms=redis_lat, error=redis_err,
                     detail="price & cooldown cache")

    # ── Binance WebSocket price feed ──────────────────────────────────
    wsh = ws_health()
    binance_ok = bool(wsh.get("ok"))
    feed_age   = wsh.get("last_update_age_sec")
    svc_binance = _svc(
        binance_ok,
        error=None if binance_ok else "Price feed stale or disconnected",
        detail=f"{len(wsh.get('prices', {}))} symbols · {f'age {feed_age:.1f}s' if feed_age is not None else 'no data'}",
        feed_age_seconds=feed_age,
        symbols_tracked=len(wsh.get("prices", {})),
    )

    # ── Telegram ──────────────────────────────────────────────────────
    tg_ok = bool(settings.telegram_bot_token)
    svc_telegram = _svc(
        tg_ok,
        error=None if tg_ok else "TELEGRAM_BOT_TOKEN not configured",
        detail="token configured" if tg_ok else None,
    )

    # ── Scanner ───────────────────────────────────────────────────────
    # Approximate last-scan time: scans fire every scan_interval_sec from boot.
    scan_iv = settings.scan_interval_sec
    elapsed = time.time() - _boot_time
    if elapsed >= scan_iv:
        completed_scans = int(elapsed / scan_iv)
        last_scan_epoch = _boot_time + completed_scans * scan_iv
        last_scan_iso   = datetime.fromtimestamp(last_scan_epoch, tz=timezone.utc).isoformat()
    else:
        last_scan_iso = None
    svc_scanner = _svc(
        True,
        detail=f"interval: {scan_iv}s · universe: {len(universe.symbols)} symbols",
        interval_seconds=scan_iv,
        last_scan_time=last_scan_iso,
    )

    # ── Worker (signal tracker) ───────────────────────────────────────
    svc_worker = _svc(
        True,
        detail="signal tracker — polls TP/SL every 30s",
        poll_seconds=30,
    )

    # ── Scheduler (stats jobs) ────────────────────────────────────────
    # daily_stats_job and weekly_stats_job exist but run on-demand /
    # via the performance rebuild endpoint, not as background tasks.
    svc_scheduler = _svc(
        True,
        detail="on-demand via /api/performance/rebuild",
    )

    # ── Activity metrics from DB ──────────────────────────────────────
    last_signal_iso: str | None = None
    signals_today   = 0
    try:
        today_start = now_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        async with SessionLocal() as s:
            # most recent signal
            res = await s.execute(
                select(Signal.created_at)
                .order_by(desc(Signal.created_at))
                .limit(1)
            )
            ts = res.scalar_one_or_none()
            if ts:
                last_signal_iso = ts.isoformat()

            # signals today
            cnt_res = await s.execute(
                select(_sqlfunc.count(Signal.id))
                .where(Signal.created_at >= today_start)
            )
            signals_today = int(cnt_res.scalar() or 0)
    except Exception:
        pass

    services = {
        "dashboard": svc_dashboard,
        "database":  svc_database,
        "redis":     svc_redis,
        "binance":   svc_binance,
        "telegram":  svc_telegram,
        "scanner":   svc_scanner,
        "worker":    svc_worker,
        "scheduler": svc_scheduler,
    }

    overall_ok = db_ok and redis_ok

    return JSONResponse({
        # ── Sprint 5 schema ────────────────────────────────────────────
        "ok":              overall_ok,
        "checked_at":      checked,
        "uptime_seconds":  uptime,
        "services":        services,
        "last_scan_time":  last_scan_iso,
        "last_signal_time":last_signal_iso,
        "signals_today":   signals_today,
        "errors_today":    0,
        # ── backward-compat (admin dashboard JS reads these) ───────────
        "brand":           "ALPHA RADAR SIGNALS",
        "uptime_sec":      uptime,
        "components": {
            "dashboard": {"ok": True, "detail": f"port {settings.dashboard_port}"},
            "database":  {"ok": db_ok,    "latency_ms": db_lat   or -1},
            "redis":     {"ok": redis_ok, "latency_ms": redis_lat or -1},
            "websocket": wsh,
        },
        "config": {
            "min_confidence":     settings.min_confidence,
            "min_rr":             settings.min_rr,
            "scan_interval_sec":  settings.scan_interval_sec,
            "max_signals_per_hour": settings.max_signals_per_hour,
            "paper_trading":      settings.paper_trading,
            "auto_trading_enabled": settings.auto_trading_enabled,
        },
    })


@app.get("/status")
async def status_route():
    wsh = ws_health()
    return {
        "status": "ok", "uptime_sec": round(time.time() - _boot_time),
        "universe": len(universe.symbols), "websocket": wsh,
        "config": {
            "min_confidence": settings.min_confidence,
            "scan_interval_sec": settings.scan_interval_sec,
            "max_signals_per_hour": settings.max_signals_per_hour,
            "timeframes": settings.timeframes,
        },
    }


@app.get("/metrics")
async def metrics():
    wsh = ws_health()
    lines = [
        "# HELP alpha_radar_universe_size Total symbols in universe",
        "# TYPE alpha_radar_universe_size gauge",
        f"alpha_radar_universe_size {len(universe.symbols)}",
        "# HELP alpha_radar_uptime_seconds Bot uptime in seconds",
        "# TYPE alpha_radar_uptime_seconds counter",
        f"alpha_radar_uptime_seconds {round(time.time() - _boot_time)}",
        "# HELP alpha_radar_ws_ok WebSocket price feed health (1=ok,0=stale)",
        "# TYPE alpha_radar_ws_ok gauge",
        f"alpha_radar_ws_ok {1 if wsh.get('ok') else 0}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ── admin API (requires auth) ─────────────────────────────────────

@app.get("/api/prices")
async def api_prices(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(ws_health())


@app.get("/api/dashboard")
async def api_dashboard(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse(await _get_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── auth routes ───────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_get():
    return _login_page()


@app.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    if not _admin_auth_configured():
        return _login_page("Admin login is disabled until DASHBOARD_USER and DASHBOARD_PASSWORD are set in .env")
    if username == _admin_user() and password == _admin_password():
        resp = RedirectResponse("/admin", status_code=302)
        resp.set_cookie("alpha_radar_auth", "ok", httponly=True, max_age=86400, samesite="lax")
        return resp
    return _login_page("Invalid username or password")


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("alpha_radar_auth")
    return resp


# ── admin dashboard ───────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request):
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(_ADMIN_HTML)


# ── public homepage ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    tg_url = _safe_url(settings.telegram_channel_url or os.getenv("TELEGRAM_CHANNEL_URL", ""))
    dc_url = _safe_url(settings.discord_url or os.getenv("DISCORD_URL", ""))
    trc20 = _safe_wallet(settings.donate_usdt_trc20 or os.getenv("DONATE_USDT_TRC20", ""))
    bep20 = _safe_wallet(settings.donate_usdt_bep20 or os.getenv("DONATE_USDT_BEP20", ""))
    btc_addr = _safe_wallet(settings.donate_btc or os.getenv("DONATE_BTC", ""))
    eth_addr = _safe_wallet(settings.donate_eth or os.getenv("DONATE_ETH", ""))
    binance_aff = _safe_url(settings.binance_affiliate_url or os.getenv("BINANCE_AFFILIATE_URL", ""))
    bybit_aff = _safe_url(settings.bybit_affiliate_url or os.getenv("BYBIT_AFFILIATE_URL", ""))
    okx_aff = _safe_url(settings.okx_affiliate_url or os.getenv("OKX_AFFILIATE_URL", ""))
    bitget_aff = _safe_url(settings.bitget_affiliate_url or os.getenv("BITGET_AFFILIATE_URL", ""))

    html = _PUBLIC_HTML

    # ── nav buttons ─────────────────────────────────────────────────
    tg_btn = (
        f'<a href="{tg_url}" target="_blank" rel="noopener" class="nav-tg">Telegram</a>'
        if tg_url else ""
    )
    dc_btn = (
        f'<a href="{dc_url}" target="_blank" rel="noopener" class="nav-dc">Discord</a>'
        if dc_url else ""
    )
    html = html.replace("__TG_BTN__", tg_btn).replace("__DC_BTN__", dc_btn)

    # ── hero CTA buttons ────────────────────────────────────────────
    hero_btns = []
    if tg_url:
        hero_btns.append(
            f'<a href="{tg_url}" target="_blank" rel="noopener" class="btn-primary">'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.96 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>'
            f'Join Telegram</a>'
        )
    if dc_url:
        hero_btns.append(
            f'<a href="{dc_url}" target="_blank" rel="noopener" class="btn-primary" style="background:#5865f2;box-shadow:none">'
            f'Join Discord</a>'
        )
    hero_btns.append(
        '<a href="/performance" class="btn-outline">&#128200; View Performance</a>'
    )
    html = html.replace("__HERO_BTNS__", "".join(hero_btns))

    # ── raw URLs for inline CTA / float button / footer ─────────────
    html = html.replace("__TG_URL__", tg_url or "#")
    html = html.replace("__DC_URL__", dc_url or "#")

    # ── footer community links ───────────────────────────────────────
    footer_comm = []
    if tg_url:
        footer_comm.append(f'<a href="{tg_url}" target="_blank" rel="noopener">Telegram</a>')
    if dc_url:
        footer_comm.append(f'<a href="{dc_url}" target="_blank" rel="noopener">Discord</a>')
    html = html.replace("__FOOTER_COMM__", "".join(footer_comm))

    # ── donation wallets ─────────────────────────────────────────────
    don_cards = []
    wallets = [
        ("USDT", "TRC20", "Tron Network", trc20, "#26a17b"),
        ("USDT", "BEP20", "BSC Network", bep20, "#f3ba2f"),
        ("BTC", "BTC", "Bitcoin", btc_addr, "#f7931a"),
        ("ETH", "ETH", "Ethereum", eth_addr, "#627eea"),
    ]
    for coin, net, netname, addr, color in wallets:
        if addr:
            safe_coin = _esc(coin)
            safe_net = _esc(net)
            safe_netname = _esc(netname)
            don_cards.append(
                f'<div class="don-card card">'
                f'<div class="don-hdr">'
                f'<span class="don-coin" style="color:{color}">{safe_coin} &mdash; {safe_net}</span>'
                f'<span class="don-net">{safe_netname}</span>'
                f'</div>'
                f'<div class="don-addr">{addr}</div>'
                f'<div class="don-acts">'
                f'<button class="don-btn don-copy" onclick="copyDonAddr(this,\'{addr}\')">Copy</button>'
                f'<button class="don-btn don-qr" onclick="showQR(\'{safe_coin}\',\'{safe_net} &mdash; {safe_netname}\',\'{addr}\')">QR Code</button>'
                f'</div></div>'
            )
    donate_section = (
        '<div class="sh">'
        '<div class="sh-lbl">&#9829; SUPPORT</div>'
        '<div class="sh-title">Support the Project</div>'
        '<div class="sh-sub">All signals are 100% free. Donations help keep the servers running 24/7.</div>'
        '</div>'
        '<div class="don-grid">' + "".join(don_cards) + '</div>'
    ) if don_cards else ""
    html = html.replace("__DONATE__", donate_section)

    # ── exchange affiliate cards ─────────────────────────────────────
    aff_cards = []
    exchanges = [
        ("Binance", binance_aff, "#f3ba2f", "B", "World's largest crypto exchange. Deepest liquidity for futures trading."),
        ("Bybit", bybit_aff, "#f7a600", "By", "Top derivatives & perpetual futures platform with low fees."),
        ("OKX", okx_aff, "#1a82ff", "OK", "Leading altcoin exchange with advanced trading tools."),
        ("Bitget", bitget_aff, "#00e6b3", "Bg", "Copy trading platform &mdash; follow top traders automatically."),
    ]
    for name, url, color, ico, desc in exchanges:
        if url:
            safe_name = _esc(name)
            aff_cards.append(
                f'<div class="exch-card card">'
                f'<div class="exch-ico" style="color:{color};border-color:{color}40;background:{color}0d">{ico}</div>'
                f'<div class="exch-name" style="color:{color}">{safe_name}</div>'
                f'<div class="exch-desc">{desc}</div>'
                f'<a href="{url}" target="_blank" rel="noopener" class="exch-btn">Register Free &rarr;</a>'
                f'</div>'
            )
    aff_section = (
        '<div class="sh">'
        '<div class="sh-lbl">&#127974; EXCHANGES</div>'
        '<div class="sh-title">Partner Exchanges</div>'
        '<div class="sh-sub">Register through our partner links to support free signals. No extra cost to you.</div>'
        '</div>'
        '<div class="exch-grid">' + "".join(aff_cards) + '</div>'
    ) if aff_cards else ""
    html = html.replace("__AFFILIATES__", aff_section)

    return HTMLResponse(html)


@app.get("/signal/{signal_id}", response_class=HTMLResponse)
async def signal_detail_page(signal_id: int):
    return HTMLResponse(_signal_detail_page_html(signal_id))


@app.get("/paper", response_class=HTMLResponse)
async def paper_page():
    return HTMLResponse(_paper_page_html())


@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page():
    return HTMLResponse(_backtest_page_html())


@app.get("/signals", response_class=HTMLResponse)
async def signals_page():
    return HTMLResponse(_signals_page_html())


@app.get("/performance", response_class=HTMLResponse)
async def performance_page():
    return HTMLResponse(_performance_page_html())


@app.get("/stats", response_class=HTMLResponse)
async def stats_page():
    return HTMLResponse(_stats_page_html())


@app.get("/about", response_class=HTMLResponse)
async def about_page():
    return HTMLResponse(_info_page(
        "About",
        """
<h2>About ALPHA RADAR SIGNALS</h2>
<p>ALPHA RADAR SIGNALS is a free, AI-powered crypto futures signal service. Our multi-timeframe analysis engine scans the market 24/7 and delivers high-quality trade setups directly to Telegram.</p>
<h3>How It Works</h3>
<ul style="color:#c9d8e8;line-height:2">
  <li><strong>1D Trend Filter</strong> — Identifies the dominant daily trend using EMA and market structure.</li>
  <li><strong>4H Structure</strong> — Confirms Break of Structure, Order Blocks, and Fair Value Gaps.</li>
  <li><strong>1H Setup</strong> — Detects pullbacks, retests, VWAP alignment, and volume confirmation.</li>
  <li><strong>15M Entry</strong> — Triggers on momentum breakout with score-based confirmation.</li>
</ul>
<h3>Signal Tiers</h3>
<ul style="color:#c9d8e8;line-height:2">
  <li><strong>ELITE (95-100%)</strong> — Highest confidence setups.</li>
  <li><strong>VIP (85-94%)</strong> — Strong setups with solid confluence.</li>
  <li><strong>PUBLIC (75-84%)</strong> — Good setups meeting minimum criteria.</li>
</ul>
<h3>Free Forever</h3>
<p>All signals are provided free of charge. We monetize through voluntary donations and affiliate partnerships.</p>
<p><a href="/faq">Frequently Asked Questions →</a></p>
""",
    ))


@app.get("/faq", response_class=HTMLResponse)
async def faq_page():
    return HTMLResponse(_info_page(
        "FAQ",
        """
<h2>Frequently Asked Questions</h2>

<h3>Are the signals free?</h3>
<p>Yes. All signals on ALPHA RADAR SIGNALS are 100% free. No subscription or payment required.</p>

<h3>How do I receive signals?</h3>
<p>Join our Telegram channel. Signals are posted automatically as soon as the AI engine detects a valid setup.</p>

<h3>What markets do you cover?</h3>
<p>We scan USDT perpetual futures on Binance — all liquid pairs with >$5M daily volume.</p>

<h3>How accurate are the signals?</h3>
<p>Accuracy varies by market conditions. Check the live performance statistics on our dashboard. Past results do not guarantee future performance.</p>

<h3>What does confidence % mean?</h3>
<p>Confidence is our AI engine's assessment of signal quality (75-100%). Higher = more confluences aligned. It is not a win probability.</p>

<h3>What is Risk/Reward (RR)?</h3>
<p>RR is the ratio of potential profit to potential loss. A 1:2.5 RR means you can gain $2.50 for every $1 risked. We require a minimum of 1:2.0.</p>

<h3>Should I use all my capital on one signal?</h3>
<p>No. Never risk more than 1-2% of your trading capital on a single trade. Proper position sizing is essential for long-term survival.</p>

<h3>Can I auto-trade these signals?</h3>
<p>Auto-trading is on our roadmap but not currently available. All signals require manual execution.</p>

<h3>Who runs this project?</h3>
<p>ALPHA RADAR SIGNALS is an independent trading tools project. We are not a registered financial institution.</p>
""",
    ))


@app.get("/terms", response_class=HTMLResponse)
async def terms():
    return HTMLResponse(_info_page(
        "Terms of Service",
        """
<h2>Terms of Service</h2>
<p>Last updated: 2026-05-30</p>
<h3>1. Acceptance</h3>
<p>By using ALPHA RADAR SIGNALS ("the Service") you agree to these Terms. If you do not agree, stop using the Service immediately.</p>
<h3>2. Educational Purpose Only</h3>
<p>All signals, analysis, and content provided by the Service are for educational and informational purposes only. Nothing on this platform constitutes financial, investment, trading, or legal advice.</p>
<h3>3. No Guarantees</h3>
<p>Past performance is not indicative of future results. Signal accuracy cannot be guaranteed. You may lose all of your capital.</p>
<h3>4. User Responsibility</h3>
<p>You are solely responsible for your trading decisions. Always conduct your own research and consult a qualified financial advisor before making any investment.</p>
<h3>5. Limitation of Liability</h3>
<p>ALPHA RADAR SIGNALS and its operators shall not be liable for any losses, damages, or costs arising from your use of the Service.</p>
<h3>6. Modifications</h3>
<p>We reserve the right to modify these Terms at any time. Continued use of the Service constitutes acceptance of the updated Terms.</p>
""",
    ))


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return HTMLResponse(_info_page(
        "Privacy Policy",
        """
<h2>Privacy Policy</h2>
<p>Last updated: 2026-05-30</p>
<h3>1. Data We Collect</h3>
<p>The public dashboard does not require account creation. We do not collect personally identifiable information from visitors to the public site.</p>
<p>If you join our Telegram channel, Telegram's own privacy policy governs the processing of your Telegram data.</p>
<h3>2. Server Logs</h3>
<p>Our servers may log IP addresses and request metadata for security monitoring and abuse prevention. These logs are retained for up to 30 days.</p>
<h3>3. Cookies</h3>
<p>The admin dashboard uses a session cookie strictly for authentication. No tracking or advertising cookies are used.</p>
<h3>4. Affiliate Links</h3>
<p>Clicking an affiliate link may set cookies on the destination exchange's website. We do not control third-party cookies.</p>
<h3>5. Contact</h3>
<p>For privacy-related inquiries please reach out via our Telegram channel.</p>
""",
    ))


@app.get("/risk-disclaimer", response_class=HTMLResponse)
async def risk_disclaimer():
    return HTMLResponse(_info_page(
        "Risk Disclaimer",
        """
<h2>Risk Disclaimer</h2>
<p><strong>TRADING FUTURES AND CRYPTOCURRENCIES INVOLVES SUBSTANTIAL RISK OF LOSS.</strong></p>
<p>You should carefully consider whether trading is appropriate for you in light of your experience, objectives, financial resources, and other relevant circumstances.</p>
<ul style="color:#c9d8e8;line-height:2">
  <li>Futures markets use leverage, which magnifies both gains and losses.</li>
  <li>You can lose more than your initial deposit in futures trading.</li>
  <li>Cryptocurrency markets are highly volatile and unregulated in many jurisdictions.</li>
  <li>Past signal performance does not guarantee future results.</li>
  <li>AI-generated signals are probabilistic tools, not certainties.</li>
  <li>Never invest money you cannot afford to lose entirely.</li>
  <li>Diversify your investments and never risk your emergency funds.</li>
  <li>ALPHA RADAR SIGNALS is not a regulated financial advisor.</li>
</ul>
<p>By using this service you acknowledge that you have read, understood, and accepted this risk disclaimer.</p>
""",
    ))


def _page_shell(title: str, body: str, extra_css: str = "", extra_js: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{_esc(title)} — ALPHA RADAR SIGNALS</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#070b12;color:#eaf2ff;font-family:Inter,Arial,sans-serif;line-height:1.6}}
a{{color:#20e6c3;text-decoration:none}}
.container{{max-width:1200px;margin:0 auto;padding:0 24px}}
header{{background:#08111c;border-bottom:1px solid #13263a;padding:13px 24px}}
.hdr{{display:flex;align-items:center;justify-content:space-between;max-width:1200px;margin:0 auto}}
.brand{{font-size:16px;font-weight:900;letter-spacing:1px;color:#eaf2ff}}
.brand em{{color:#20f0c0;font-style:normal}}
.hnav{{display:flex;gap:16px;font-size:13px}}
.page-title{{font-size:28px;font-weight:900;color:#20f0c0;padding:32px 0 8px}}
.card{{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:20px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #17283d;font-size:13px}}
th{{color:#8fa8c7;font-size:10px;letter-spacing:1px;text-transform:uppercase}}
tr:last-child td{{border-bottom:none}}
.g{{color:#20ff80}}.r{{color:#ff4f61}}.c{{color:#20e6c3}}.y{{color:#ffd84d}}
.bl2{{background:#0a3a1f44;color:#20ff80;border:1px solid #20ff8033;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}}
.bs2{{background:#3a0a1244;color:#ff4f61;border:1px solid #ff4f6133;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}}
.bopen{{color:#20ffc8;font-weight:700}}.btp{{color:#20ff80;font-weight:700}}.bsl{{color:#ff4f61;font-weight:700}}.bexp{{color:#ffd84d;font-weight:700}}
.sbar{{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:20px 0}}
.scard{{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:19px;text-align:center}}
.slabel{{color:#7fa0c8;font-size:10px;letter-spacing:2px;text-transform:uppercase}}
.sval{{font-size:32px;font-weight:900;margin-top:8px}}
footer{{border-top:1px solid #13263a;padding:26px 24px;text-align:center;color:#627a99;font-size:12px;margin-top:32px}}
@media(max-width:860px){{.sbar{{grid-template-columns:1fr 1fr}}}}
@media(max-width:480px){{.sbar{{grid-template-columns:1fr}}}}
{extra_css}
</style>
</head>
<body>
<header>
<div class="hdr">
  <div class="brand"><a href="/" style="text-decoration:none;color:#eaf2ff">ALPHA RADAR <em>SIGNALS</em></a></div>
  <div class="hnav">
    <a href="/signals">Signals</a>
    <a href="/performance">Performance</a>
    <a href="/paper">Paper</a>
    <a href="/backtest">Backtest</a>
    <a href="/health">Health</a>
    <a href="/faq">FAQ</a>
  </div>
</div>
</header>
<div class="container">
{body}
</div>
<footer>
  <p>ALPHA RADAR SIGNALS &nbsp;·&nbsp; Free AI-powered crypto futures signals</p>
  <p style="margin-top:6px">
    <a href="/terms" style="color:#627a99">Terms</a> &nbsp;·&nbsp;
    <a href="/privacy" style="color:#627a99">Privacy</a> &nbsp;·&nbsp;
    <a href="/risk-disclaimer" style="color:#627a99">Risk Disclaimer</a>
  </p>
</footer>
<script>{extra_js}</script>
</body>
</html>"""


def _signals_page_html() -> str:
    body = """
<div class="page-title">Live Signals</div>
<div class="sbar">
  <div class="scard"><div class="slabel">OPEN NOW</div><div id="s-open" class="sval c">—</div></div>
  <div class="scard"><div class="slabel">WIN RATE (7D)</div><div id="s-wr" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">SIGNALS (7D)</div><div id="s-tot" class="sval">—</div></div>
  <div class="scard"><div class="slabel">AVG PNL</div><div id="s-pnl" class="sval g">—</div></div>
</div>
<div class="card" style="overflow-x:auto">
  <div style="display:flex;gap:8px;margin-bottom:14px">
    <button class="tbtn act" onclick="swTab('all',this)">All</button>
    <button class="tbtn" onclick="swTab('open',this)">Open</button>
    <button class="tbtn" onclick="swTab('closed',this)">Closed</button>
  </div>
  <div id="tp-all" style="overflow-x:auto">
  <table>
  <thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>ENTRY</th><th>TP1</th><th>SL</th><th>STATUS</th><th>PNL</th></tr></thead>
  <tbody id="tbl-all"><tr><td colspan="11" style="text-align:center;color:#627a99;padding:24px">Loading...</td></tr></tbody>
  </table>
  </div>
  <div id="tp-open" style="display:none;overflow-x:auto">
  <table>
  <thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>ENTRY</th><th>TP1</th><th>SL</th></tr></thead>
  <tbody id="tbl-open"></tbody>
  </table>
  </div>
  <div id="tp-closed" style="display:none;overflow-x:auto">
  <table>
  <thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
  <tbody id="tbl-closed"></tbody>
  </table>
  </div>
</div>"""
    js = """
function sBadge(st){
  if(st==='OPEN')return '<span class="bopen">OPEN</span>';
  if(st==='SL')return '<span class="bsl">SL</span>';
  if(st==='EXPIRED')return '<span class="bexp">EXP</span>';
  return '<span class="btp">'+st+'</span>';
}
function pct(v){return v===null||v===undefined?'—':(v>=0?'+':'')+v+'%';}
function cls(v){return v>=0?'g':'r';}
let _all=[];
function swTab(n,btn){
  document.querySelectorAll('.tbtn').forEach(b=>b.classList.remove('act'));
  btn.classList.add('act');
  ['all','open','closed'].forEach(id=>{
    document.getElementById('tp-'+id).style.display=id===n?'':'none';
  });
}
async function load(){
  const r=await fetch('/api/public/signals?limit=200');
  if(!r.ok)return;
  _all=await r.json();
  const open=_all.filter(x=>x.status==='OPEN');
  const closed=_all.filter(x=>x.status!=='OPEN');
  function rowAll(x){
    return '<tr><td>'+x.created_at.slice(5,16).replace('T',' ')+'</td>'+
      '<td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.timeframe+'</td><td>'+x.confidence+'%</td><td>1:'+x.risk_reward+'</td>'+
      '<td>'+x.entry_low+'</td><td>'+x.tp1+'</td><td>'+x.sl+'</td>'+
      '<td>'+sBadge(x.status)+'</td>'+
      '<td class="'+cls(x.pnl_pct)+'">'+pct(x.pnl_pct)+'</td></tr>';
  }
  function rowOpen(x){
    return '<tr><td>'+x.created_at.slice(5,16).replace('T',' ')+'</td>'+
      '<td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.timeframe+'</td><td>'+x.confidence+'%</td><td>1:'+x.risk_reward+'</td>'+
      '<td>'+x.entry_low+'</td><td>'+x.tp1+'</td><td>'+x.sl+'</td></tr>';
  }
  function rowClosed(x){
    return '<tr><td>'+x.created_at.slice(5,16).replace('T',' ')+'</td>'+
      '<td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.timeframe+'</td><td>'+x.confidence+'%</td><td>1:'+x.risk_reward+'</td>'+
      '<td>'+sBadge(x.status)+'</td>'+
      '<td class="'+cls(x.pnl_pct)+'">'+pct(x.pnl_pct)+'</td></tr>';
  }
  document.getElementById('tbl-all').innerHTML=_all.map(rowAll).join('')||'<tr><td colspan="11" style="text-align:center;color:#627a99;padding:20px">No signals yet</td></tr>';
  document.getElementById('tbl-open').innerHTML=open.map(rowOpen).join('')||'<tr><td colspan="9" style="text-align:center;color:#627a99;padding:20px">No open signals</td></tr>';
  document.getElementById('tbl-closed').innerHTML=closed.map(rowClosed).join('')||'<tr><td colspan="8" style="text-align:center;color:#627a99;padding:20px">No closed signals</td></tr>';
  document.getElementById('s-open').textContent=open.length;
}
async function loadStats(){
  const r=await fetch('/api/public/stats');
  if(!r.ok)return;
  const d=await r.json();
  document.getElementById('s-wr').textContent=d.winrate+'%';
  document.getElementById('s-tot').textContent=d.signals7d;
  const pe=document.getElementById('s-pnl');
  pe.textContent=pct(d.avgpnl);pe.className='sval '+cls(d.avgpnl);
}
document.addEventListener('DOMContentLoaded',()=>{
  load();loadStats();
  setInterval(load,15000);setInterval(loadStats,15000);
});
var style=document.createElement('style');
style.textContent='.tbtn{padding:7px 15px;border-radius:7px;border:1px solid #17314b;background:transparent;color:#8fa8c7;cursor:pointer;font-size:12px}.tbtn.act{background:#08a98f22;border-color:#20f0c0;color:#20f0c0}';
document.head.appendChild(style);
"""
    return _page_shell("Live Signals", body, extra_js=js)


def _performance_page_html() -> str:
    css = """
/* ── performance center ───────────────────────────────────── */
.pc-header{margin-bottom:20px}
.pc-title{font-size:26px;font-weight:900;letter-spacing:1px;color:#eaf2ff}
.pc-subtitle{font-size:12px;color:#7fa0c8;margin-top:4px;letter-spacing:1px}
.pc-warn{background:#1a140833;border:1px solid #ffd84d55;border-radius:9px;
  padding:10px 16px;margin-bottom:20px;font-size:12px;color:#ffd84d;
  display:flex;align-items:center;gap:8px}
.pc-kpi{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:20px}
.pc-kcard{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;
  border-radius:12px;padding:16px;text-align:center}
.pc-klbl{font-size:9px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.pc-kval{font-size:26px;font-weight:900}
.pc-sides{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}
.pc-side-card{background:linear-gradient(180deg,#101827,#0b1320);
  border:1px solid #17314b;border-radius:12px;padding:18px}
.pc-side-title{font-size:13px;font-weight:700;margin-bottom:12px;
  padding-bottom:8px;border-bottom:1px solid #17314b}
.pc-srow{display:flex;justify-content:space-between;align-items:center;
  padding:7px 0;border-bottom:1px solid #0e1e2e;font-size:13px}
.pc-srow:last-child{border-bottom:none}
.pc-slbl{color:#7fa0c8;font-size:12px}
.pc-sval{font-weight:700;color:#eaf2ff}
.pc-filter{display:flex;gap:6px;margin-bottom:12px}
.pc-fbtn{padding:6px 14px;border-radius:7px;border:1px solid #17314b;
  background:transparent;color:#8fa8c7;cursor:pointer;font-size:12px;transition:all .15s}
.pc-fbtn.act{background:#08a98f22;border-color:#20f0c0;color:#20f0c0}
.pc-empty{background:#0b1320;border:1px solid #17314b;border-radius:12px;
  padding:40px;text-align:center;color:#627a99;font-size:14px;margin-bottom:20px}
@media(max-width:900px){.pc-kpi{grid-template-columns:repeat(3,1fr)}}
@media(max-width:560px){.pc-kpi{grid-template-columns:repeat(2,1fr)}.pc-sides{grid-template-columns:1fr}}
"""
    body = """
<div class="pc-header">
  <div class="pc-title">ALPHA RADAR PERFORMANCE CENTER</div>
  <div class="pc-subtitle">MTF ENGINE ONLY &nbsp;·&nbsp; strategy = MTF_SMC_STRICT &nbsp;·&nbsp; 15m / 1H / 4H / 1D</div>
</div>

<div class="pc-warn">
  ⚠&nbsp; Legacy 5m signals are excluded from this report. Only MTF_SMC_STRICT / 15m–1D signals are included.
</div>

<div class="pc-kpi">
  <div class="pc-kcard">
    <div class="pc-klbl">Total Signals</div>
    <div id="pc-total" class="pc-kval" style="color:#eaf2ff">—</div>
    <div id="pc-open-sub" style="font-size:10px;color:#627a99;margin-top:3px"></div>
  </div>
  <div class="pc-kcard">
    <div class="pc-klbl">Win Rate</div>
    <div id="pc-wr" class="pc-kval g">—</div>
    <div id="pc-lr-sub" style="font-size:10px;color:#627a99;margin-top:3px"></div>
  </div>
  <div class="pc-kcard">
    <div class="pc-klbl">Profit Factor</div>
    <div id="pc-pf" class="pc-kval c">—</div>
  </div>
  <div class="pc-kcard">
    <div class="pc-klbl">Avg PnL / Trade</div>
    <div id="pc-avgpnl" class="pc-kval g">—</div>
  </div>
  <div class="pc-kcard">
    <div class="pc-klbl">Avg RR</div>
    <div id="pc-rr" class="pc-kval y">—</div>
  </div>
  <div class="pc-kcard">
    <div class="pc-klbl">Avg Hold Time</div>
    <div id="pc-hold" class="pc-kval" style="color:#eaf2ff">—</div>
  </div>
</div>

<div id="pc-no-data" class="pc-empty" style="display:none">
  Not enough closed trades yet — check back after the first signals close.
</div>

<div id="pc-content">
  <div class="pc-sides">
    <div class="pc-side-card" id="pc-long-card">
      <div class="pc-side-title" style="color:#20ff80">LONG</div>
      <div id="pc-long-rows"></div>
    </div>
    <div class="pc-side-card" id="pc-short-card">
      <div class="pc-side-title" style="color:#ff4f61">SHORT</div>
      <div id="pc-short-rows"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:18px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <div style="font-size:13px;font-weight:700;color:#eaf2ff">Symbol Leaderboard</div>
      <div class="pc-filter">
        <button class="pc-fbtn act" onclick="lbFilter('all',this)">All</button>
        <button class="pc-fbtn" onclick="lbFilter('long',this)">LONG</button>
        <button class="pc-fbtn" onclick="lbFilter('short',this)">SHORT</button>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table>
    <thead><tr>
      <th>SYMBOL</th><th>TOTAL</th><th>WINS</th><th>LOSSES</th>
      <th>WIN RATE</th><th>AVG PNL</th><th>TOTAL PNL</th><th>AVG RR</th>
    </tr></thead>
    <tbody id="lb-tbl">
      <tr><td colspan="8" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr>
    </tbody>
    </table>
    </div>
  </div>

  <div class="card">
    <div style="font-size:13px;font-weight:700;color:#eaf2ff;margin-bottom:12px">Monthly Performance</div>
    <div style="overflow-x:auto">
    <table>
    <thead><tr>
      <th>MONTH</th><th>SIGNALS</th><th>WINS</th><th>LOSSES</th>
      <th>WIN RATE</th><th>TOTAL PNL</th><th>PROFIT FACTOR</th>
    </tr></thead>
    <tbody id="monthly-tbl">
      <tr><td colspan="7" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr>
    </tbody>
    </table>
    </div>
  </div>
</div>
"""
    js = """
let _lbData = [];
let _lbMode = 'all';

function _p(v){ return v===null||v===undefined?'—':(v>=0?'+':'')+v+'%'; }
function _cls(v){ return v>=0?'g':'r'; }
function _pf(v){ return v===null||v===undefined?'∞':v; }
function _fmtHold(m){
  if(m===null||m===undefined)return'—';
  const h=Math.floor(m/60), mn=Math.round(m%60);
  return h>0?h+'h '+mn+'m':mn+'m';
}

function _sideRows(st){
  const rows=[
    ['Total',    `<b>${st.total}</b>`],
    ['Wins',     `<span class="g">${st.wins}</span>`],
    ['Losses',   `<span class="r">${st.losses}</span>`],
    ['Win Rate', `<b class="${_cls(st.win_rate-50)}">${st.win_rate}%</b>`],
    ['Avg PnL',  `<span class="${_cls(st.avg_pnl)}">${_p(st.avg_pnl)}</span>`],
    ['Avg RR',   `<span class="y">1:${st.avg_rr}</span>`],
  ];
  return rows.map(([l,v])=>
    `<div class="pc-srow"><span class="pc-slbl">${l}</span><span class="pc-sval">${v}</span></div>`
  ).join('');
}

function lbFilter(mode, btn){
  _lbMode = mode;
  document.querySelectorAll('.pc-fbtn').forEach(b=>b.classList.remove('act'));
  btn.classList.add('act');
  _renderLb();
}

function _renderLb(){
  const rows = _lbData;
  const empty = '<tr><td colspan="8" style="text-align:center;color:#627a99;padding:18px">No data yet</td></tr>';
  if(!rows.length){ document.getElementById('lb-tbl').innerHTML=empty; return; }

  document.getElementById('lb-tbl').innerHTML = rows.map((x,i)=>{
    let total, wins, losses, avgPnl, totalPnl;
    if(_lbMode==='long'){
      const l=x.long||{};
      total=l.total||0; wins=l.wins||0; losses=total-wins;
      avgPnl=l.avg_pnl; totalPnl=null;
    }else if(_lbMode==='short'){
      const s=x.short||{};
      total=s.total||0; wins=s.wins||0; losses=total-wins;
      avgPnl=s.avg_pnl; totalPnl=null;
    }else{
      total=x.total; wins=x.wins; losses=x.losses;
      avgPnl=x.avg_pnl; totalPnl=x.total_pnl;
    }
    if(total===0)return'';
    const wr = total>0?Math.round(wins/total*100):0;
    return `<tr>
      <td><b>${x.symbol}</b></td>
      <td>${total}</td>
      <td class="g">${wins}</td>
      <td class="r">${losses}</td>
      <td class="${wr>=50?'g':'r'}">${wr}%</td>
      <td class="${_cls(avgPnl)}">${_p(avgPnl)}</td>
      <td class="${totalPnl!==null?_cls(totalPnl):''}">
        ${totalPnl!==null?_p(totalPnl):'—'}
      </td>
      <td class="y">${_lbMode==='all'?'1:'+x.avg_rr:'—'}</td>
    </tr>`;
  }).filter(Boolean).join('')||empty;
}

async function load(){
  const r = await fetch('/api/public/performance');
  if(!r.ok) return;
  const d = await r.json();
  if(d.error) return;

  const noData = (d.closed_signals||d.total_closed||0) === 0;
  document.getElementById('pc-no-data').style.display = noData ? '' : 'none';
  document.getElementById('pc-content').style.display  = noData ? 'none' : '';

  // KPI cards
  const tot = d.total_signals ?? (d.total_closed+d.open_signals||0);
  document.getElementById('pc-total').textContent = tot || '—';
  document.getElementById('pc-open-sub').textContent =
    d.open_signals != null ? d.open_signals + ' open' : '';

  const wrEl = document.getElementById('pc-wr');
  wrEl.textContent = (d.win_rate??'—') + (d.win_rate!=null?'%':'');
  wrEl.className = 'pc-kval ' + (d.win_rate>=50?'g':'r');
  document.getElementById('pc-lr-sub').textContent =
    d.loss_rate != null ? d.loss_rate + '% loss rate' : '';

  document.getElementById('pc-pf').textContent = _pf(d.profit_factor);
  const pnlEl = document.getElementById('pc-avgpnl');
  pnlEl.textContent = _p(d.avg_pnl);
  pnlEl.className = 'pc-kval ' + _cls(d.avg_pnl);
  document.getElementById('pc-rr').textContent = d.avg_rr!=null?'1:'+d.avg_rr:'—';
  document.getElementById('pc-hold').textContent = _fmtHold(d.avg_hold_time_minutes??d.avg_hold_min);

  // LONG / SHORT side cards
  if(d.long)  document.getElementById('pc-long-rows').innerHTML  = _sideRows(d.long);
  if(d.short) document.getElementById('pc-short-rows').innerHTML = _sideRows(d.short);

  // Symbol leaderboard
  _lbData = d.symbol_leaderboard || d.leaderboard?.map(x=>({
    symbol:x.symbol,total:x.count,wins:0,losses:0,
    win_rate:0,avg_pnl:x.avg,total_pnl:0,avg_rr:0,
    long:{total:0,wins:0,avg_pnl:0},short:{total:0,wins:0,avg_pnl:0}
  })) || [];
  _renderLb();

  // Monthly table
  const mo = (d.monthly||[]).slice().reverse();
  const mEmpty = '<tr><td colspan="7" style="text-align:center;color:#627a99;padding:18px">No monthly data yet</td></tr>';
  document.getElementById('monthly-tbl').innerHTML = mo.length
    ? mo.map(m=>`<tr>
        <td>${m.month}</td>
        <td>${m.signals}</td>
        <td class="g">${m.wins}</td>
        <td class="r">${m.losses}</td>
        <td class="${m.win_rate>=50?'g':'r'}">${m.win_rate}%</td>
        <td class="${_cls(m.total_pnl)}">${_p(m.total_pnl)}</td>
        <td class="c">${_pf(m.profit_factor)}</td>
      </tr>`).join('')
    : mEmpty;
}

document.addEventListener('DOMContentLoaded', load);
"""
    return _page_shell("Performance Center", body, extra_css=css, extra_js=js)


def _stats_page_html() -> str:
    body = """
<div class="page-title">Statistics Overview</div>
<div class="sbar">
  <div class="scard"><div class="slabel">WIN RATE (7D)</div><div id="wr" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">SIGNALS (7D)</div><div id="tot" class="sval">—</div></div>
  <div class="scard"><div class="slabel">OPEN SIGNALS</div><div id="open" class="sval c">—</div></div>
  <div class="scard"><div class="slabel">UNIVERSE</div><div id="uni" class="sval y">—</div></div>
</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
  <div class="card" style="text-align:center">
    <div class="slabel">WINS</div>
    <div id="wins" class="sval g" style="font-size:40px;margin:12px 0">—</div>
  </div>
  <div class="card" style="text-align:center">
    <div class="slabel">LOSSES</div>
    <div id="losses" class="sval r" style="font-size:40px;margin:12px 0">—</div>
  </div>
</div>
<div class="card">
  <div style="font-size:14px;font-weight:700;margin-bottom:14px;color:#eaf2ff">Recent Signals</div>
  <div style="overflow-x:auto">
  <table>
  <thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
  <tbody id="sig-tbl"><tr><td colspan="8" style="text-align:center;color:#627a99;padding:24px">Loading...</td></tr></tbody>
  </table>
  </div>
</div>"""
    js = """
function sBadge(st){
  if(st==='OPEN')return '<span class="bopen">OPEN</span>';
  if(st==='SL')return '<span class="bsl">SL</span>';
  if(st==='EXPIRED')return '<span class="bexp">EXP</span>';
  return '<span class="btp">'+st+'</span>';
}
function pct(v){return v===null||v===undefined?'—':(v>=0?'+':'')+v+'%';}
function cls(v){return v>=0?'g':'r';}
async function load(){
  const r=await fetch('/api/public/stats');
  if(!r.ok)return;
  const d=await r.json();
  document.getElementById('wr').textContent=d.winrate+'%';
  document.getElementById('tot').textContent=d.signals7d;
  document.getElementById('open').textContent=d.open_signals;
  document.getElementById('uni').textContent=d.universe;
  document.getElementById('wins').textContent=d.wins;
  document.getElementById('losses').textContent=d.losses;
  document.getElementById('sig-tbl').innerHTML=(d.recent||[]).map(x=>
    '<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td>'+
    '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
    '<td>'+x.tf+'</td><td>'+x.conf+'%</td><td>1:'+x.rr+'</td>'+
    '<td>'+sBadge(x.status)+'</td>'+
    '<td class="'+cls(x.pnl)+'">'+pct(x.pnl)+'</td></tr>'
  ).join('')||'<tr><td colspan="8" style="text-align:center;color:#627a99;padding:20px">No signals yet</td></tr>';
}
document.addEventListener('DOMContentLoaded',()=>{load();setInterval(load,10000);});
"""
    return _page_shell("Stats", body, extra_js=js)


def _info_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{_esc(title)} — ALPHA RADAR SIGNALS</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#070b12;color:#eaf2ff;font-family:Inter,Arial,sans-serif;line-height:1.7}}
.container{{max-width:820px;margin:0 auto;padding:48px 24px}}
h2{{color:#20f0c0;margin-bottom:18px;font-size:22px}}
h3{{color:#eaf2ff;margin:24px 0 8px;font-size:16px}}
p{{color:#c9d8e8;margin-bottom:12px;font-size:14px}}
ul{{padding-left:24px;margin-bottom:12px}}
.back{{margin-top:36px}}
a{{color:#20e6c3}}
header{{background:#08111c;border-bottom:1px solid #13263a;padding:13px 24px}}
.brand{{font-size:16px;font-weight:900;letter-spacing:1px;color:#eaf2ff}}
.brand em{{color:#20f0c0;font-style:normal}}
</style>
</head>
<body>
<header>
  <div class="brand"><a href="/" style="text-decoration:none;color:#eaf2ff">ALPHA RADAR <em>SIGNALS</em></a></div>
</header>
<div class="container">
{body}
<div class="back"><a href="/">← Back to Home</a></div>
</div>
</body>
</html>"""


def _signal_detail_page_html(signal_id: int) -> str:
    css = """
/* ── signal detail ────────────────────────────────────────── */
.sd-hero{background:linear-gradient(135deg,#0b1a2e,#0d2238);border:1px solid #17314b;
  border-radius:14px;padding:22px 24px;margin-bottom:20px;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px}
.sd-symbol{font-size:28px;font-weight:900;letter-spacing:1px;color:#eaf2ff}
.sd-meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sd-tf{background:#0b1320;border:1px solid #17314b;border-radius:6px;
  padding:4px 10px;font-size:11px;color:#8fa8c7;letter-spacing:1px}
.sd-status-box{text-align:right}
.sd-status-lbl{font-size:10px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px}
.sd-status-val{font-size:22px;font-weight:900}
.sd-status-open{color:#20ffc8}
.sd-status-tp{color:#20ff80}
.sd-status-sl{color:#ff4f61}
.sd-status-exp{color:#ffd84d}
.sd-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.sd-section-title{font-size:11px;font-weight:700;letter-spacing:2px;
  text-transform:uppercase;color:#7fa0c8;margin-bottom:14px;
  padding-bottom:8px;border-bottom:1px solid #17314b}
.sd-row{display:flex;justify-content:space-between;align-items:center;
  padding:9px 0;border-bottom:1px solid #0e1e2e}
.sd-row:last-child{border-bottom:none}
.sd-lbl{font-size:12px;color:#7fa0c8}
.sd-val{font-size:13px;font-weight:700;color:#eaf2ff;text-align:right}
.sd-entry{background:#08182a;border:1px solid #17314b;border-radius:8px;padding:10px 14px;
  display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.sd-entry-lbl{font-size:10px;color:#7fa0c8;letter-spacing:1px;text-transform:uppercase}
.sd-entry-zone{font-size:13px;font-weight:700;color:#20e6c3}
.sd-level{display:flex;justify-content:space-between;align-items:center;
  padding:8px 0;border-bottom:1px solid #0e1e2e}
.sd-level:last-child{border-bottom:none}
.sd-level-lbl{display:flex;align-items:center;gap:8px;font-size:12px;color:#7fa0c8}
.sd-level-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sd-score-row{margin-bottom:14px}
.sd-score-row:last-child{margin-bottom:0}
.sd-score-header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.sd-score-name{font-size:12px;color:#8fa8c7}
.sd-score-val{font-size:14px;font-weight:900;color:#20e6c3}
.sd-score-bar{background:#0b1320;border-radius:4px;height:7px;overflow:hidden}
.sd-score-fill{background:linear-gradient(90deg,#08a98f,#20f0c0);height:100%;border-radius:4px;
  transition:width .6s ease}
.sd-reason-item{display:flex;align-items:flex-start;gap:10px;
  padding:9px 0;border-bottom:1px solid #0e1e2e;font-size:13px;color:#c9d8e8}
.sd-reason-item:last-child{border-bottom:none}
.sd-reason-dot{width:6px;height:6px;border-radius:50%;background:#20f0c0;
  margin-top:5px;flex-shrink:0}
.sd-pnl-banner{border-radius:10px;padding:14px 18px;margin-bottom:16px;
  display:flex;align-items:center;justify-content:space-between}
.sd-pnl-banner.win{background:#0a3a1f55;border:1px solid #20ff8055}
.sd-pnl-banner.loss{background:#3a0a1255;border:1px solid #ff4f6155}
.sd-pnl-banner.open{background:#083a3255;border:1px solid #20ffc855}
.sd-back{display:inline-flex;align-items:center;gap:6px;color:#20e6c3;
  font-size:13px;font-weight:600;padding:8px 0;text-decoration:none}
.sd-back:hover{color:#20f0c0}
@media(max-width:640px){.sd-grid{grid-template-columns:1fr}.sd-hero{flex-direction:column}}
"""
    body = f"""
<div style="margin-bottom:12px">
  <a href="/signals" class="sd-back">← Back to Signals</a>
</div>
<div id="sd-root" style="color:#8fa8c7;padding:32px;text-align:center">Loading signal #{signal_id}…</div>
"""
    js = f"""
const _SID = {signal_id};

function _fmt(v) {{
  if (v === null || v === undefined) return '—';
  return parseFloat(v).toPrecision(7).replace(/\\.?0+$/, '');
}}
function _pct(v) {{
  if (v === null || v === undefined) return '—';
  return (v >= 0 ? '+' : '') + parseFloat(v).toFixed(2) + '%';
}}
function _scoreBar(val, max, label, sublabel) {{
  if (val === null || val === undefined) {{
    return `<div class="sd-score-row">
      <div class="sd-score-header">
        <span class="sd-score-name">${{label}}</span>
        <span style="font-size:12px;color:#627a99">N/A</span>
      </div>
      <div class="sd-score-bar"><div class="sd-score-fill" style="width:0%"></div></div>
    </div>`;
  }}
  const pct = Math.min(100, Math.round(val / max * 100));
  const barColor = pct >= 80 ? 'linear-gradient(90deg,#08a98f,#20f0c0)'
                : pct >= 50 ? 'linear-gradient(90deg,#0e7a6e,#1abda0)'
                : 'linear-gradient(90deg,#1a4a42,#0e6050)';
  return `<div class="sd-score-row">
    <div class="sd-score-header">
      <span class="sd-score-name">${{label}} <span style="color:#627a99;font-size:10px">${{sublabel}}</span></span>
      <span class="sd-score-val">${{val}} <span style="font-size:11px;color:#627a99">/ ${{max}}</span></span>
    </div>
    <div class="sd-score-bar">
      <div class="sd-score-fill" style="width:${{pct}}%;background:${{barColor}}"></div>
    </div>
  </div>`;
}}
function _statusClass(st) {{
  if (st === 'OPEN')   return 'sd-status-open';
  if (st === 'SL')     return 'sd-status-sl';
  if (st === 'EXPIRED' || st === 'CANCELLED') return 'sd-status-exp';
  return 'sd-status-tp';
}}
function _sideBadge(side) {{
  return side === 'LONG'
    ? '<span class="bl2">LONG</span>'
    : '<span class="bs2">SHORT</span>';
}}

async function load() {{
  const r = await fetch('/api/public/signal/' + _SID);
  if (!r.ok) {{
    document.getElementById('sd-root').innerHTML =
      '<p style="color:#ff4f61;font-size:15px">Signal #' + _SID + ' not found.</p>' +
      '<a href="/signals" class="sd-back" style="margin-top:12px">← Back to Signals</a>';
    return;
  }}
  const d = await r.json();
  if (d.error) {{
    document.getElementById('sd-root').innerHTML =
      '<p style="color:#ff4f61">' + d.error + '</p>';
    return;
  }}

  const isWin  = ['TP1','TP2','TP3'].includes(d.status);
  const isLoss = d.status === 'SL';
  const isOpen = d.status === 'OPEN';
  const pnlClass = isOpen ? 'open' : isWin ? 'win' : isLoss ? 'loss' : 'open';

  // PnL banner
  const pnlBanner = `<div class="sd-pnl-banner ${{pnlClass}}">
    <div>
      <div style="font-size:10px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:3px">
        ${{isOpen ? 'Unrealised PnL' : 'Final PnL'}}
      </div>
      <div style="font-size:24px;font-weight:900;color:${{isOpen?'#20ffc8':isWin?'#20ff80':'#ff4f61'}}">
        ${{_pct(d.pnl_pct)}}
      </div>
    </div>
    <div class="sd-status-box">
      <div class="sd-status-lbl">Status</div>
      <div class="sd-status-val ${{_statusClass(d.status)}}">${{d.status}}</div>
    </div>
  </div>`;

  // Signal info rows
  const infoRows = [
    ['Symbol',      `<b style="font-size:15px">${{d.symbol}}</b>`],
    ['Side',        _sideBadge(d.side)],
    ['Timeframe',   `<span class="sd-tf">${{d.timeframe}}</span>`],
    ['Confidence',  `<span class="c" style="font-size:15px;font-weight:900">${{d.confidence}}%</span>`],
    ['Risk/Reward', `<span class="y">1&nbsp;:&nbsp;${{d.risk_reward}}</span>`],
    ['Risk Level',  `<span style="color:#8fa8c7">${{d.risk_level||'—'}}</span>`],
    ['Opened',      `<span style="color:#8fa8c7">${{(d.created_at||'').slice(0,16).replace('T',' ')}}</span>`],
    d.closed_at
      ? ['Closed', `<span style="color:#8fa8c7">${{(d.closed_at||'').slice(0,16).replace('T',' ')}}</span>`]
      : null,
  ].filter(Boolean).map(([lbl, val]) =>
    `<div class="sd-row"><span class="sd-lbl">${{lbl}}</span><span class="sd-val">${{val}}</span></div>`
  ).join('');

  // Levels
  const levelsHtml = `
    <div class="sd-entry">
      <div>
        <div class="sd-entry-lbl">Entry Zone</div>
        <div class="sd-entry-zone">${{_fmt(d.entry_low)}} → ${{_fmt(d.entry_high)}}</div>
      </div>
    </div>
    <div class="sd-level">
      <div class="sd-level-lbl">
        <div class="sd-level-dot" style="background:#ff4f61"></div>Stop Loss
      </div>
      <span style="color:#ff4f61;font-weight:700;font-size:13px">${{_fmt(d.stop_loss)}}</span>
    </div>
    <div class="sd-level">
      <div class="sd-level-lbl">
        <div class="sd-level-dot" style="background:#20c97a"></div>TP1
      </div>
      <span style="color:#20c97a;font-weight:700;font-size:13px">${{_fmt(d.tp1)}}</span>
    </div>
    <div class="sd-level">
      <div class="sd-level-lbl">
        <div class="sd-level-dot" style="background:#20ff80"></div>TP2
      </div>
      <span style="color:#20ff80;font-weight:700;font-size:13px">${{_fmt(d.tp2)}}</span>
    </div>
    <div class="sd-level">
      <div class="sd-level-lbl">
        <div class="sd-level-dot" style="background:#4dffa0"></div>TP3
      </div>
      <span style="color:#4dffa0;font-weight:700;font-size:13px">${{_fmt(d.tp3)}}</span>
    </div>`;

  // MTF scores
  const scoresHtml =
    _scoreBar(d.trend_score,     20, '1D Trend',      '(EMA + structure, max 20)') +
    _scoreBar(d.structure_score,  5, '4H Structure',   '(confluence hits, max 5)') +
    _scoreBar(d.setup_score,      5, '1H Setup',       '(setup hits, max 5)') +
    _scoreBar(d.entry_score,     10, '15M Entry',      '(weighted triggers, max 10)');

  // Reasoning
  const reasons = (d.reasons || []);
  const reasonsHtml = reasons.length
    ? reasons.map(r =>
        `<div class="sd-reason-item"><div class="sd-reason-dot"></div><span>${{r}}</span></div>`
      ).join('')
    : '<p style="color:#627a99;font-size:13px;padding:8px 0">No reasoning recorded for this signal.</p>';

  document.getElementById('sd-root').innerHTML = `
    <div class="sd-hero">
      <div class="sd-meta">
        <span class="sd-symbol">${{d.symbol}}</span>
        ${{_sideBadge(d.side)}}
        <span class="sd-tf">${{d.timeframe}}</span>
        <span class="sd-tf">#{signal_id}</span>
      </div>
      <div class="sd-status-box">
        <div class="sd-status-lbl">Confidence</div>
        <div class="sd-status-val" style="color:#20e6c3">${{d.confidence}}%</div>
      </div>
    </div>

    ${{pnlBanner}}

    <div class="sd-grid">
      <div class="card">
        <div class="sd-section-title">Signal Info</div>
        ${{infoRows}}
      </div>
      <div class="card">
        <div class="sd-section-title">Levels</div>
        ${{levelsHtml}}
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="sd-section-title">MTF Layer Scores</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
        <div>
          ${{_scoreBar(d.trend_score, 20, '1D Trend', '(max 20)')}}
          ${{_scoreBar(d.structure_score, 5, '4H Structure', '(max 5)')}}
        </div>
        <div>
          ${{_scoreBar(d.setup_score, 5, '1H Setup', '(max 5)')}}
          ${{_scoreBar(d.entry_score, 10, '15M Entry', '(max 10)')}}
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:20px">
      <div class="sd-section-title">Reasoning</div>
      ${{reasonsHtml}}
    </div>

    <a href="/signals" class="sd-back">← Back to Signals</a>
  `;
}}

document.addEventListener('DOMContentLoaded', load);
"""
    return _page_shell(f"Signal #{signal_id}", body, extra_css=css, extra_js=js)


def _health_page_html() -> str:
    css = """
/* ── health center ────────────────────────────────────────── */
.hc-header{margin-bottom:20px}
.hc-title{font-size:26px;font-weight:900;letter-spacing:1px;color:#eaf2ff}
.hc-subtitle{font-size:12px;color:#7fa0c8;margin-top:4px;letter-spacing:1px}
.hc-kpi{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px}
.hc-kcard{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;
  border-radius:12px;padding:15px;text-align:center}
.hc-klbl{font-size:9px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.hc-kval{font-size:22px;font-weight:900}
.hc-kval.g{color:#20ff80}.hc-kval.r{color:#ff4f61}.hc-kval.c{color:#20e6c3}
.hc-kval.y{color:#ffd84d}.hc-kval.w{color:#eaf2ff}
.hc-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.hc-svc{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;
  border-radius:12px;padding:16px}
.hc-svc-name{font-size:10px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px}
.hc-svc-status{font-size:18px;font-weight:900;margin-bottom:6px}
.hc-svc-status.online{color:#20ff80}
.hc-svc-status.offline{color:#ff4f61}
.hc-svc-detail{font-size:10px;color:#8fa8c7;line-height:1.5}
.hc-svc-lat{display:inline-block;background:#0b1320;border:1px solid #17314b;border-radius:4px;
  padding:2px 7px;font-size:10px;color:#20e6c3;margin-top:4px;font-family:monospace}
.hc-svc-err{font-size:10px;color:#ff6b6b;margin-top:4px;word-break:break-word}
.hc-svc-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;
  vertical-align:middle}
.hc-act-card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;
  border-radius:12px;padding:18px;margin-bottom:16px}
.hc-act-title{font-size:12px;font-weight:700;color:#eaf2ff;margin-bottom:12px;
  padding-bottom:8px;border-bottom:1px solid #17314b;letter-spacing:1px;text-transform:uppercase}
.hc-row{display:flex;justify-content:space-between;align-items:center;
  padding:8px 0;border-bottom:1px solid #0e1e2e;font-size:13px}
.hc-row:last-child{border-bottom:none}
.hc-rlbl{color:#7fa0c8;font-size:12px}
.hc-rval{font-weight:700;color:#eaf2ff;text-align:right}
.hc-cfg-grid{display:grid;grid-template-columns:1fr 1fr;gap:0}
.hc-checked{font-size:10px;color:#627a99;text-align:right;margin-top:4px}
@media(max-width:900px){.hc-grid{grid-template-columns:repeat(2,1fr)}.hc-kpi{grid-template-columns:repeat(3,1fr)}}
@media(max-width:560px){.hc-grid{grid-template-columns:1fr 1fr}.hc-kpi{grid-template-columns:repeat(2,1fr)}}
"""
    body = """
<div class="hc-header">
  <div class="hc-title">HEALTH CENTER</div>
  <div class="hc-subtitle">ALPHA RADAR SIGNALS &nbsp;·&nbsp; System Status</div>
</div>

<div class="hc-kpi">
  <div class="hc-kcard">
    <div class="hc-klbl">Overall</div>
    <div id="hc-overall" class="hc-kval c">—</div>
  </div>
  <div class="hc-kcard">
    <div class="hc-klbl">Uptime</div>
    <div id="hc-uptime" class="hc-kval y">—</div>
  </div>
  <div class="hc-kcard">
    <div class="hc-klbl">Signals Today</div>
    <div id="hc-sig-today" class="hc-kval w">—</div>
  </div>
  <div class="hc-kcard">
    <div class="hc-klbl">Errors Today</div>
    <div id="hc-err-today" class="hc-kval g">—</div>
  </div>
  <div class="hc-kcard">
    <div class="hc-klbl">Universe</div>
    <div id="hc-universe" class="hc-kval c">—</div>
  </div>
</div>

<div class="hc-grid" id="hc-svc-grid">
  <!-- 8 service cards injected by JS -->
</div>

<div class="hc-act-card">
  <div class="hc-act-title">Activity</div>
  <div class="hc-row">
    <span class="hc-rlbl">Last Scan Time</span>
    <span id="hc-last-scan" class="hc-rval">—</span>
  </div>
  <div class="hc-row">
    <span class="hc-rlbl">Last Signal Time</span>
    <span id="hc-last-sig" class="hc-rval">—</span>
  </div>
  <div class="hc-row">
    <span class="hc-rlbl">Scanner Interval</span>
    <span id="hc-scan-iv" class="hc-rval">—</span>
  </div>
  <div class="hc-row">
    <span class="hc-rlbl">Signals Today</span>
    <span id="hc-sig-today-2" class="hc-rval">—</span>
  </div>
  <div class="hc-row">
    <span class="hc-rlbl">Errors Today</span>
    <span id="hc-err-today-2" class="hc-rval g">—</span>
  </div>
</div>

<div class="hc-act-card">
  <div class="hc-act-title">Configuration</div>
  <div id="hc-cfg"></div>
</div>

<div id="hc-checked-at" class="hc-checked"></div>
"""
    js = """
function _fmtUp(sec){
  const d=Math.floor(sec/86400),h=Math.floor((sec%86400)/3600),
        m=Math.floor((sec%3600)/60);
  return d>0?d+'d '+h+'h '+m+'m':h>0?h+'h '+m+'m':m+'m '+(sec%60)+'s';
}
function _fmtTime(iso){
  if(!iso)return'—';
  const t=new Date(iso);
  return t.toLocaleDateString()+' '+t.toLocaleTimeString();
}
function _fmtAgo(iso){
  if(!iso)return'—';
  const sec=Math.round((Date.now()-new Date(iso).getTime())/1000);
  if(sec<60)return sec+'s ago';
  if(sec<3600)return Math.floor(sec/60)+'m ago';
  return Math.floor(sec/3600)+'h '+Math.floor((sec%3600)/60)+'m ago';
}

const SVC_LABELS = {
  dashboard:'Dashboard', database:'Database', redis:'Redis',
  binance:'Binance', telegram:'Telegram', scanner:'Scanner',
  worker:'Worker', scheduler:'Scheduler'
};

function _buildSvcCard(key, svc){
  const ok = svc.ok;
  const dot = `<span class="hc-svc-dot" style="background:${ok?'#20ff80':'#ff4f61'}"></span>`;
  const lat = svc.latency_ms!=null
    ? `<span class="hc-svc-lat">${svc.latency_ms}ms</span>` : '';
  const det = svc.detail
    ? `<div class="hc-svc-detail">${svc.detail}</div>` : '';
  const err = !ok && svc.error
    ? `<div class="hc-svc-err">⚠ ${svc.error}</div>` : '';
  const chk = `<div class="hc-svc-detail" style="color:#4a6278;margin-top:4px">checked ${_fmtAgo(svc.checked_at)}</div>`;
  return `<div class="hc-svc">
    <div class="hc-svc-name">${SVC_LABELS[key]||key}</div>
    <div class="hc-svc-status ${ok?'online':'offline'}">${dot}${ok?'ONLINE':'OFFLINE'}</div>
    ${det}${lat}${err}${chk}
  </div>`;
}

async function load(){
  try{
    const r=await fetch('/api/health');
    if(!r.ok)return;
    const d=await r.json();

    // KPI bar
    const ok=d.ok;
    const ov=document.getElementById('hc-overall');
    ov.textContent=ok?'ALL OK':'DEGRADED';
    ov.className='hc-kval '+(ok?'g':'r');
    document.getElementById('hc-uptime').textContent=_fmtUp(d.uptime_seconds??d.uptime_sec??0);
    document.getElementById('hc-sig-today').textContent=d.signals_today??'—';
    const errEl=document.getElementById('hc-err-today');
    errEl.textContent=d.errors_today??0;
    errEl.className='hc-kval '+(d.errors_today>0?'r':'g');
    document.getElementById('hc-universe').textContent=
      d.services?.binance?.symbols_tracked??d.components?.websocket?.prices
        ?Object.keys(d.components?.websocket?.prices||{}).length:'—';

    // Service cards
    const svcs=d.services||{};
    const ORDER=['dashboard','database','redis','binance','telegram','scanner','worker','scheduler'];
    document.getElementById('hc-svc-grid').innerHTML=
      ORDER.map(k=>k in svcs?_buildSvcCard(k,svcs[k]):'').join('');

    // Activity
    document.getElementById('hc-last-scan').textContent=
      d.last_scan_time?_fmtTime(d.last_scan_time):'—';
    document.getElementById('hc-last-sig').textContent=
      d.last_signal_time?_fmtTime(d.last_signal_time)+' ('+_fmtAgo(d.last_signal_time)+')':'None';
    document.getElementById('hc-scan-iv').textContent=
      (d.config?.scan_interval_sec??d.services?.scanner?.interval_seconds??'—')+'s';
    document.getElementById('hc-sig-today-2').textContent=d.signals_today??'—';
    const e2=document.getElementById('hc-err-today-2');
    e2.textContent=d.errors_today??0;
    e2.className='hc-rval '+(d.errors_today>0?'r':'g');

    // Config
    if(d.config){
      const cfg=d.config;
      document.getElementById('hc-cfg').innerHTML=`
        <div class="hc-row"><span class="hc-rlbl">Min Confidence</span><span class="hc-rval">${cfg.min_confidence}%</span></div>
        <div class="hc-row"><span class="hc-rlbl">Min RR</span><span class="hc-rval">1:${cfg.min_rr}</span></div>
        <div class="hc-row"><span class="hc-rlbl">Scan Interval</span><span class="hc-rval">${cfg.scan_interval_sec}s</span></div>
        <div class="hc-row"><span class="hc-rlbl">Max Signals/hr</span><span class="hc-rval">${cfg.max_signals_per_hour}</span></div>
        <div class="hc-row"><span class="hc-rlbl">Paper Trading</span><span class="hc-rval">${cfg.paper_trading?'<span class="y">ON</span>':'OFF'}</span></div>
        <div class="hc-row"><span class="hc-rlbl">Auto Trading</span><span class="hc-rval">${cfg.auto_trading_enabled?'<span class="y">ON</span>':'<span style="color:#627a99">Disabled</span>'}</span></div>
      `;
    }

    document.getElementById('hc-checked-at').textContent=
      'Last checked: '+new Date(d.checked_at||Date.now()).toLocaleTimeString();

  }catch(e){console.error('health load error',e);}
}

document.addEventListener('DOMContentLoaded',()=>{
  load();
  setInterval(load,15000);
});
"""
    return _page_shell("Health Center", body, extra_css=css, extra_js=js)


def _paper_page_html() -> str:
    css = """
/* ── paper trading ────────────────────────────────────────── */
.pp-header{margin-bottom:16px}
.pp-title{font-size:26px;font-weight:900;letter-spacing:1px;color:#eaf2ff}
.pp-subtitle{font-size:12px;color:#7fa0c8;margin-top:4px}
.pp-warn{background:#0a2a0a55;border:1px solid #20ff8033;border-radius:9px;
  padding:10px 16px;margin-bottom:18px;font-size:12px;color:#8fa8c7}
.pp-kpi{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:18px}
.pp-kcard{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;
  border-radius:12px;padding:15px;text-align:center}
.pp-klbl{font-size:9px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.pp-kval{font-size:22px;font-weight:900}
.pp-curve-wrap{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;
  border-radius:12px;padding:16px;margin-bottom:18px}
.pp-curve-title{font-size:11px;font-weight:700;color:#7fa0c8;letter-spacing:2px;
  text-transform:uppercase;margin-bottom:12px}
.pp-curve{display:flex;align-items:flex-end;gap:3px;height:70px;overflow:hidden}
.pp-curve-bar{flex:1;min-width:4px;border-radius:2px 2px 0 0;opacity:0.85}
@media(max-width:720px){.pp-kpi{grid-template-columns:repeat(3,1fr)}}
@media(max-width:460px){.pp-kpi{grid-template-columns:repeat(2,1fr)}}
"""
    body = """
<div class="pp-header">
  <div class="pp-title">PAPER TRADING</div>
  <div class="pp-subtitle">Virtual Portfolio · 10 000 USDT · 1% Risk Per Trade · No Real Funds</div>
</div>

<div class="pp-warn">
  📊 <b style="color:#20ff80">Simulated only.</b>
  Positions are opened automatically for every valid MTF signal.
  No real Binance API calls. No real money.
</div>

<div class="pp-kpi">
  <div class="pp-kcard">
    <div class="pp-klbl">Balance</div>
    <div id="pp-bal" class="pp-kval c">—</div>
    <div id="pp-bal-sub" style="font-size:10px;color:#627a99;margin-top:3px"></div>
  </div>
  <div class="pp-kcard">
    <div class="pp-klbl">Total PnL</div>
    <div id="pp-pnl" class="pp-kval g">—</div>
    <div id="pp-pnl-pct" style="font-size:10px;color:#627a99;margin-top:3px"></div>
  </div>
  <div class="pp-kcard">
    <div class="pp-klbl">Win Rate</div>
    <div id="pp-wr" class="pp-kval g">—</div>
    <div id="pp-wr-sub" style="font-size:10px;color:#627a99;margin-top:3px"></div>
  </div>
  <div class="pp-kcard">
    <div class="pp-klbl">Open</div>
    <div id="pp-open" class="pp-kval y">—</div>
  </div>
  <div class="pp-kcard">
    <div class="pp-klbl">Closed</div>
    <div id="pp-closed" class="pp-kval w">—</div>
    <div id="pp-wl" style="font-size:10px;color:#627a99;margin-top:3px"></div>
  </div>
</div>

<div class="pp-curve-wrap">
  <div class="pp-curve-title">Balance Curve</div>
  <div class="pp-curve" id="pp-curve">
    <div style="color:#627a99;font-size:12px;padding:8px">Loading...</div>
  </div>
</div>

<div class="card" style="margin-bottom:16px">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
    <div style="font-size:13px;font-weight:700;color:#eaf2ff">
      Open Positions <span id="pp-open-cnt" style="color:#ffd84d;font-size:13px">(—)</span>
    </div>
  </div>
  <div style="overflow-x:auto">
  <table>
  <thead><tr>
    <th>OPENED</th><th>SYMBOL</th><th>SIDE</th>
    <th>ENTRY</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th>
    <th>SIZE</th><th>STATUS</th>
  </tr></thead>
  <tbody id="pp-open-tbl">
    <tr><td colspan="10" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr>
  </tbody>
  </table>
  </div>
</div>

<div class="card">
  <div style="font-size:13px;font-weight:700;color:#eaf2ff;margin-bottom:12px">Closed Trades</div>
  <div style="overflow-x:auto">
  <table>
  <thead><tr>
    <th>OPENED</th><th>CLOSED</th><th>SYMBOL</th><th>SIDE</th>
    <th>ENTRY</th><th>SL</th><th>TP1</th>
    <th>SIZE</th><th>STATUS</th><th>PNL %</th><th>PNL USDT</th>
  </tr></thead>
  <tbody id="pp-closed-tbl">
    <tr><td colspan="11" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr>
  </tbody>
  </table>
  </div>
</div>
"""
    js = """
function _p(v){return(v>=0?'+':'')+v+'%';}
function _cls(v){return v>=0?'g':'r';}
function _u(v){return(v>=0?'+$':'−$')+Math.abs(v).toFixed(2);}
function _badge(st){
  if(st==='OPEN')  return '<span class="bopen">OPEN</span>';
  if(st==='SL')    return '<span class="bsl">SL</span>';
  if(['TP1','TP2','TP3'].includes(st)) return '<span class="btp">'+st+'</span>';
  return '<span class="bexp">'+st+'</span>';
}
function _side(s){ return s==='LONG'?'<span class="bl2">LONG</span>':'<span class="bs2">SHORT</span>'; }

async function load(){
  try{
    const r=await fetch('/api/paper');
    if(!r.ok)return;
    const d=await r.json();
    if(d.error)return;

    // KPI
    document.getElementById('pp-bal').textContent='$'+(d.current_balance||0).toFixed(2);
    document.getElementById('pp-bal-sub').textContent='started $'+(d.initial_balance||10000).toFixed(0);
    const pEl=document.getElementById('pp-pnl');
    pEl.textContent=_u(d.total_pnl_usdt||0);
    pEl.className='pp-kval '+_cls(d.total_pnl_usdt||0);
    document.getElementById('pp-pnl-pct').textContent=_p(d.total_pnl_pct||0);
    const wrEl=document.getElementById('pp-wr');
    wrEl.textContent=(d.win_rate||0)+'%';
    wrEl.className='pp-kval '+(d.win_rate>=50?'g':'r');
    document.getElementById('pp-wr-sub').textContent=(d.wins||0)+'W / '+(d.losses||0)+'L';
    document.getElementById('pp-open').textContent=d.open_count||0;
    document.getElementById('pp-open-cnt').textContent='('+d.open_count+')';
    document.getElementById('pp-closed').textContent=d.closed_count||0;
    document.getElementById('pp-wl').textContent=(d.wins||0)+'W / '+(d.losses||0)+'L';

    // Balance curve
    const curve=d.balance_curve||[];
    if(curve.length>1){
      const mn=Math.min(...curve),mx=Math.max(...curve),range=mx-mn||1;
      document.getElementById('pp-curve').innerHTML=curve.map(v=>{
        const h=Math.max(4,Math.round((v-mn)/range*66));
        const col=v>=d.initial_balance?'#20ff80':'#ff4f61';
        return`<div class="pp-curve-bar" style="height:${h}px;background:${col}"></div>`;
      }).join('');
    }else{
      document.getElementById('pp-curve').innerHTML=
        '<p style="color:#627a99;font-size:12px">Not enough closed trades yet</p>';
    }

    // Open positions
    const empty10='<tr><td colspan="10" style="text-align:center;color:#627a99;padding:14px">No open positions</td></tr>';
    document.getElementById('pp-open-tbl').innerHTML=(d.open||[]).map(x=>`<tr>
      <td>${x.opened_at||'—'}</td>
      <td><b><a href="/signal/${x.signal_id||x.id}" style="color:#20e6c3">${x.symbol}</a></b></td>
      <td>${_side(x.side)}</td>
      <td class="c">${x.entry_price}</td>
      <td class="r">${x.stop_loss}</td>
      <td class="g">${x.tp1}</td>
      <td class="g">${x.tp2||'—'}</td>
      <td class="g">${x.tp3||'—'}</td>
      <td style="color:#ffd84d">$${x.size_usdt}</td>
      <td>${_badge(x.status)}</td>
    </tr>`).join('')||empty10;

    // Closed trades
    const empty11='<tr><td colspan="11" style="text-align:center;color:#627a99;padding:14px">No closed trades yet</td></tr>';
    document.getElementById('pp-closed-tbl').innerHTML=(d.closed||[]).map(x=>`<tr>
      <td>${x.opened_at||'—'}</td>
      <td>${x.closed_at||'—'}</td>
      <td><b><a href="/signal/${x.signal_id||x.id}" style="color:#20e6c3">${x.symbol}</a></b></td>
      <td>${_side(x.side)}</td>
      <td class="c">${x.entry_price}</td>
      <td class="r">${x.stop_loss}</td>
      <td class="g">${x.tp1}</td>
      <td style="color:#ffd84d">$${x.size_usdt}</td>
      <td>${_badge(x.status)}</td>
      <td class="${_cls(x.pnl_pct)}">${_p(x.pnl_pct)}</td>
      <td class="${_cls(x.pnl_usdt)}">${_u(x.pnl_usdt)}</td>
    </tr>`).join('')||empty11;

  }catch(e){console.error('paper load error',e);}
}
document.addEventListener('DOMContentLoaded',()=>{load();setInterval(load,15000);});
"""
    return _page_shell("Paper Trading", body, extra_css=css, extra_js=js)


def _backtest_page_html() -> str:
    css = """
/* ── historical backtest ──────────────────────────────────── */
.bk-header{margin-bottom:20px}
.bk-kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.bk-kcard{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;
  border-radius:12px;padding:15px;text-align:center}
.bk-klbl{font-size:9px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.bk-kval{font-size:24px;font-weight:900}
.bk-2col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.bk-sec-title{font-size:11px;font-weight:700;color:#7fa0c8;letter-spacing:2px;
  text-transform:uppercase;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #17314b}
.bk-row{display:flex;justify-content:space-between;padding:8px 0;
  border-bottom:1px solid #0e1e2e;font-size:13px}
.bk-row:last-child{border-bottom:none}
.bk-rlbl{color:#7fa0c8;font-size:12px}
.bk-rval{font-weight:700;color:#eaf2ff}
.bk-curve{display:flex;align-items:flex-end;gap:2px;height:90px;overflow:hidden;margin-top:4px}
.bk-no-data{color:#627a99;font-size:13px;padding:24px;text-align:center}
/* form */
.bt-form{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;align-items:end;margin-bottom:6px}
.bt-field label{display:block;font-size:9px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px}
.bt-field input,.bt-field select{width:100%;background:#07101a;border:1px solid #17314b;border-radius:8px;
  color:#eaf2ff;padding:9px 11px;font-size:13px;outline:none}
.bt-field input:focus,.bt-field select:focus{border-color:#20f0c0}
.bt-run{background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;border:none;
  border-radius:8px;font-weight:900;font-size:13px;padding:10px 22px;cursor:pointer;
  width:100%;letter-spacing:1px}
.bt-run:disabled{opacity:0.45;cursor:not-allowed}
.bt-loading{background:#0a1f14;border:1px solid #20f0c044;border-radius:10px;
  padding:18px;text-align:center;color:#20f0c0;font-size:13px;display:none;margin-bottom:16px}
.bt-spinner{display:inline-block;width:14px;height:14px;border:2px solid #20f0c033;
  border-top-color:#20f0c0;border-radius:50%;animation:spin 0.8s linear infinite;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
/* legacy section */
.bk-legacy-title{font-size:10px;font-weight:700;color:#475d78;letter-spacing:2px;text-transform:uppercase;
  margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #0e1e2e}
@media(max-width:720px){.bk-kpi{grid-template-columns:repeat(2,1fr)}.bk-2col{grid-template-columns:1fr}.bt-form{grid-template-columns:1fr}}
"""
    body = """
<div class="page-title">Historical Backtest</div>

<!-- ── Control panel ────────────────────────────────────────── -->
<div class="card" style="margin-bottom:16px">
  <div class="bk-sec-title">Simulation Parameters</div>
  <div class="bt-form">
    <div class="bt-field">
      <label>Symbol</label>
      <input id="bt-sym" value="BTCUSDT" placeholder="e.g. BTCUSDT" maxlength="20">
    </div>
    <div class="bt-field">
      <label>Start Date</label>
      <input id="bt-start" type="date">
    </div>
    <div class="bt-field">
      <label>End Date</label>
      <input id="bt-end" type="date">
    </div>
    <div class="bt-field">
      <label>Strategy</label>
      <select id="bt-strat">
        <option value="V3.2">V3.2 — MTF_SMC_STRICT</option>
      </select>
    </div>
    <div class="bt-field">
      <label>&nbsp;</label>
      <button id="bt-btn" class="bt-run" onclick="runBacktest()">▶ RUN BACKTEST</button>
    </div>
  </div>
  <div style="font-size:10px;color:#475d78;margin-top:4px">
    Replays Binance klines candle-by-candle · max 366 days · may take 20–60 s
  </div>
</div>

<!-- ── Loading indicator ─────────────────────────────────────── -->
<div id="bt-loading" class="bt-loading">
  <span class="bt-spinner"></span>Running historical simulation — please wait…
</div>

<!-- ── Results (hidden until a run completes) ────────────────── -->
<div id="bt-results" style="display:none">
  <div id="bt-meta" style="font-size:11px;color:#7fa0c8;margin-bottom:12px"></div>

  <div class="bk-kpi">
    <div class="bk-kcard">
      <div class="bk-klbl">Trades</div>
      <div id="r-total" class="bk-kval">—</div>
      <div id="r-wl" style="font-size:10px;color:#627a99;margin-top:3px"></div>
    </div>
    <div class="bk-kcard">
      <div class="bk-klbl">Win Rate</div>
      <div id="r-wr" class="bk-kval g">—</div>
    </div>
    <div class="bk-kcard">
      <div class="bk-klbl">Profit Factor</div>
      <div id="r-pf" class="bk-kval c">—</div>
    </div>
    <div class="bk-kcard">
      <div class="bk-klbl">Max Drawdown</div>
      <div id="r-dd" class="bk-kval r">—</div>
    </div>
    <div class="bk-kcard">
      <div class="bk-klbl">Sharpe Ratio</div>
      <div id="r-sh" class="bk-kval y">—</div>
    </div>
    <div class="bk-kcard">
      <div class="bk-klbl">Avg RR</div>
      <div id="r-rr" class="bk-kval y">—</div>
    </div>
    <div class="bk-kcard">
      <div class="bk-klbl">Avg PnL / Trade</div>
      <div id="r-pnl" class="bk-kval g">—</div>
    </div>
    <div class="bk-kcard">
      <div class="bk-klbl">Total PnL</div>
      <div id="r-tpnl" class="bk-kval g">—</div>
    </div>
  </div>

  <!-- Equity curve -->
  <div class="card" style="margin-bottom:16px">
    <div class="bk-sec-title">Equity Curve <span style="font-size:9px;color:#627a99">(Cumulative PnL %)</span></div>
    <div id="r-curve" class="bk-curve"><div class="bk-no-data">—</div></div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#627a99;margin-top:6px">
      <span>Trade 1</span><span id="r-curve-end"></span>
    </div>
  </div>

  <div class="bk-2col">
    <!-- Summary stats -->
    <div class="card">
      <div class="bk-sec-title">Summary Statistics</div>
      <div id="r-summary"></div>
    </div>
    <!-- RR distribution -->
    <div class="card">
      <div class="bk-sec-title">RR Distribution</div>
      <div id="r-rrdist"></div>
    </div>
  </div>

  <!-- Monthly table -->
  <div class="card" style="margin-bottom:16px">
    <div class="bk-sec-title">Monthly Returns</div>
    <div style="overflow-x:auto">
    <table>
    <thead><tr>
      <th>MONTH</th><th>TRADES</th><th>WINS</th><th>LOSSES</th>
      <th>WIN RATE</th><th>TOTAL PNL</th><th>PROFIT FACTOR</th>
    </tr></thead>
    <tbody id="r-monthly">
      <tr><td colspan="7" style="text-align:center;color:#627a99;padding:14px">—</td></tr>
    </tbody>
    </table>
    </div>
  </div>

  <!-- Trade list -->
  <div class="card">
    <div class="bk-sec-title">Trade Log <span id="r-trade-count" style="font-size:9px;color:#627a99"></span></div>
    <div style="overflow-x:auto">
    <table>
    <thead><tr>
      <th>ENTRY TIME</th><th>SIDE</th><th>ENTRY</th><th>EXIT</th>
      <th>STATUS</th><th>PNL</th><th>CONF</th><th>RR</th><th>HOLD</th>
    </tr></thead>
    <tbody id="r-trades">
      <tr><td colspan="9" style="text-align:center;color:#627a99;padding:14px">—</td></tr>
    </tbody>
    </table>
    </div>
  </div>
</div>

<!-- ── Legacy: DB signal metrics ────────────────────────────── -->
<div class="card" style="margin-top:28px;opacity:0.7">
  <div class="bk-legacy-title">Signal DB — All-time Closed Trades (V3 MTF_SMC_STRICT)</div>
  <div id="leg-inner"><div class="bk-no-data">Loading…</div></div>
</div>
"""
    js = """
// ── helpers ────────────────────────────────────────────────────
function _p(v){ return v===null||v===undefined?'—':(v>=0?'+':'')+parseFloat(v).toFixed(2)+'%'; }
function _cls(v){ return parseFloat(v||0)>=0?'g':'r'; }
function _pf(v){ return v===null||v===undefined?'∞':parseFloat(v).toFixed(2); }
function _fmt(iso){
  if(!iso) return '—';
  const d=new Date(iso);
  return d.toISOString().slice(0,16).replace('T',' ');
}

// ── Set default dates ──────────────────────────────────────────
(function(){
  const t=new Date();
  const end=new Date(t); end.setDate(end.getDate()-1);
  const start=new Date(t); start.setDate(start.getDate()-91);
  const fmt=d=>d.toISOString().slice(0,10);
  document.getElementById('bt-end').value=fmt(end);
  document.getElementById('bt-start').value=fmt(start);
})();

// ── Run historical backtest ────────────────────────────────────
async function runBacktest(){
  const sym   = (document.getElementById('bt-sym').value||'').trim().toUpperCase();
  const start = document.getElementById('bt-start').value;
  const end   = document.getElementById('bt-end').value;
  const strat = document.getElementById('bt-strat').value;
  if(!sym||!start||!end){ alert('Fill in all fields'); return; }

  const btn=document.getElementById('bt-btn');
  btn.disabled=true;
  document.getElementById('bt-loading').style.display='block';
  document.getElementById('bt-results').style.display='none';

  try{
    const url=`/api/backtest/run?symbol=${encodeURIComponent(sym)}&start=${start}&end=${end}&strategy=${strat}`;
    const r=await fetch(url);
    const d=await r.json();
    if(!r.ok||d.error){
      alert('Error: '+(d.error||r.statusText));
      return;
    }
    renderResults(d);
  }catch(e){
    alert('Request failed: '+e.message);
  }finally{
    btn.disabled=false;
    document.getElementById('bt-loading').style.display='none';
  }
}

// ── Render results ─────────────────────────────────────────────
function renderResults(d){
  // Meta line
  document.getElementById('bt-meta').textContent =
    `${d.symbol} · ${d.start_date} → ${d.end_date} · ${d.strategy_version} · `+
    `${d.candles_scanned||0} candles scanned · ${d.signals_generated||0} signals generated`;

  // KPIs
  document.getElementById('r-total').textContent = d.total_trades||0;
  document.getElementById('r-wl').textContent = (d.wins||0)+'W / '+(d.losses||0)+'L / '+(d.expired||0)+'EXP';
  const wrEl=document.getElementById('r-wr');
  wrEl.textContent=(d.win_rate||0)+'%';
  wrEl.className='bk-kval '+((d.win_rate||0)>=50?'g':'r');
  document.getElementById('r-pf').textContent=_pf(d.profit_factor);
  document.getElementById('r-dd').textContent=_p(-(d.max_drawdown_pct||0));
  document.getElementById('r-sh').textContent=parseFloat(d.sharpe_ratio||0).toFixed(2);
  document.getElementById('r-rr').textContent=d.avg_rr?'1:'+parseFloat(d.avg_rr).toFixed(2):'—';
  const pEl=document.getElementById('r-pnl');
  pEl.textContent=_p(d.avg_pnl); pEl.className='bk-kval '+_cls(d.avg_pnl);
  const tEl=document.getElementById('r-tpnl');
  tEl.textContent=_p(d.total_pnl); tEl.className='bk-kval '+_cls(d.total_pnl);

  // Equity curve
  const curve=d.equity_curve||[];
  const curveEl=document.getElementById('r-curve');
  if(curve.length>1){
    const mn=Math.min(...curve),mx=Math.max(...curve),rng=mx-mn||1;
    curveEl.innerHTML=curve.map(v=>{
      const h=Math.max(3,Math.round((v-mn)/rng*86));
      return `<div style="flex:1;min-width:2px;height:${h}px;background:${v>=0?'#20ff80':'#ff4f61'};border-radius:1px 1px 0 0;opacity:0.85"></div>`;
    }).join('');
    document.getElementById('r-curve-end').textContent='Trade '+(curve.length-1);
  } else {
    curveEl.innerHTML='<div class="bk-no-data">Not enough trades</div>';
  }

  // Summary stats
  const n=d.total_trades||0;
  document.getElementById('r-summary').innerHTML = n===0
    ? '<div class="bk-no-data">No closed trades in this period</div>'
    : `<div class="bk-row"><span class="bk-rlbl">Total Trades</span><span class="bk-rval">${n}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Wins</span><span class="bk-rval g">${d.wins}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Losses</span><span class="bk-rval r">${d.losses}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Expired</span><span class="bk-rval y">${d.expired}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Win Rate</span><span class="bk-rval ${_cls(d.win_rate-50)}">${d.win_rate}%</span></div>
       <div class="bk-row"><span class="bk-rlbl">Profit Factor</span><span class="bk-rval c">${_pf(d.profit_factor)}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Sharpe Ratio</span><span class="bk-rval y">${parseFloat(d.sharpe_ratio||0).toFixed(2)}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Max Drawdown</span><span class="bk-rval r">${_p(-(d.max_drawdown_pct||0))}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Avg RR</span><span class="bk-rval y">1:${parseFloat(d.avg_rr||0).toFixed(2)}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Avg PnL/Trade</span><span class="bk-rval ${_cls(d.avg_pnl)}">${_p(d.avg_pnl)}</span></div>
       <div class="bk-row"><span class="bk-rlbl">Total PnL</span><span class="bk-rval ${_cls(d.total_pnl)}">${_p(d.total_pnl)}</span></div>`;

  // RR distribution
  const rrd=d.rr_distribution||[];
  const maxC=Math.max(1,...rrd.map(x=>x.count));
  document.getElementById('r-rrdist').innerHTML=rrd.length
    ? rrd.map(x=>`
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:12px">
          <div style="width:36px;color:#8fa8c7;text-align:right;font-weight:700">1:${x.rr}</div>
          <div style="flex:1;background:#0b1320;border-radius:4px;height:14px;overflow:hidden">
            <div style="background:linear-gradient(90deg,#08a98f,#20f0c0);width:${Math.round(x.count/maxC*100)}%;height:100%;border-radius:4px"></div>
          </div>
          <div style="width:28px;color:#eaf2ff;font-weight:700;text-align:right">${x.count}</div>
        </div>`).join('')
    : '<div class="bk-no-data">No data</div>';

  // Monthly table
  const mo=(d.monthly||[]).slice().reverse();
  document.getElementById('r-monthly').innerHTML=mo.length
    ? mo.map(m=>`<tr>
        <td>${m.month}</td><td>${m.signals}</td>
        <td class="g">${m.wins}</td><td class="r">${m.losses}</td>
        <td class="${m.win_rate>=50?'g':'r'}">${m.win_rate}%</td>
        <td class="${_cls(m.total_pnl)}">${_p(m.total_pnl)}</td>
        <td class="c">${_pf(m.profit_factor)}</td>
      </tr>`).join('')
    : '<tr><td colspan="7" style="text-align:center;color:#627a99;padding:14px">No data</td></tr>';

  // Trade log
  const trades=d.trades||[];
  document.getElementById('r-trade-count').textContent='('+trades.length+' shown)';
  const statusCls=s=>({'TP1':'g','TP2':'g','TP3':'g','SL':'r','EXPIRED':'y'}[s]||'');
  document.getElementById('r-trades').innerHTML=trades.length
    ? trades.map(t=>`<tr>
        <td style="font-size:11px;color:#7fa0c8">${_fmt(t.entry_time)}</td>
        <td class="${t.side==='LONG'?'g':'r'}">${t.side}</td>
        <td style="font-size:11px">${parseFloat(t.entry_price).toPrecision(6)}</td>
        <td style="font-size:11px">${parseFloat(t.exit_price).toPrecision(6)}</td>
        <td class="${statusCls(t.status)}" style="font-weight:700">${t.status}</td>
        <td class="${_cls(t.pnl_pct)}" style="font-weight:700">${_p(t.pnl_pct)}</td>
        <td style="font-size:11px;color:#7fa0c8">${parseFloat(t.confidence||0).toFixed(0)}%</td>
        <td style="font-size:11px;color:#7fa0c8">1:${parseFloat(t.risk_reward||0).toFixed(2)}</td>
        <td style="font-size:11px;color:#7fa0c8">${t.hold_candles}×15m</td>
      </tr>`).join('')
    : '<tr><td colspan="9" style="text-align:center;color:#627a99;padding:14px">No trades</td></tr>';

  document.getElementById('bt-results').style.display='block';
}

// ── Legacy DB backtest (all-time closed) ───────────────────────
async function loadLegacy(){
  try{
    const r=await fetch('/api/backtest');
    if(!r.ok) return;
    const d=await r.json();
    if(d.error||!d.total){ document.getElementById('leg-inner').innerHTML='<div class="bk-no-data">No closed signals in DB yet</div>'; return; }
    document.getElementById('leg-inner').innerHTML=
      `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px">
        ${[['Trades',d.total],['Win Rate',d.win_rate+'%'],['Profit Factor',_pf(d.profit_factor)],
           ['Sharpe',d.sharpe_ratio],['Max DD',_p(-d.max_drawdown)],['Avg PnL',_p(d.avg_pnl)]].map(([l,v])=>
          `<div style="background:#0a111a;border:1px solid #0e1e2e;border-radius:8px;padding:12px;text-align:center">
             <div style="font-size:9px;color:#475d78;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px">${l}</div>
             <div style="font-size:18px;font-weight:900;color:#7fa0c8">${v}</div>
           </div>`).join('')}
      </div>`;
  }catch(e){}
}

document.addEventListener('DOMContentLoaded', loadLegacy);
"""
    return _page_shell("Historical Backtest", body, extra_css=css, extra_js=js)


def create_app():
    return app


# ═════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═════════════════════════════════════════════════════════════════

_LOGIN_HTML = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Admin Login — ALPHA RADAR SIGNALS</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#070b12;color:#eaf2ff;font-family:Arial}
.box{width:360px;background:#0b1320;border:1px solid #17314b;border-radius:18px;padding:28px;box-shadow:0 0 35px #00ffc822}
.logo{width:64px;height:64px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:32px;margin:auto}
h1{text-align:center;color:#20f0c0;font-size:22px;margin:16px 0 4px}
p{text-align:center;color:#8fa8c7;margin-bottom:22px;font-size:13px}
input{width:100%;padding:13px;margin:7px 0;border-radius:9px;border:1px solid #17314b;background:#07101a;color:#fff;box-sizing:border-box}
button{width:100%;padding:13px;margin-top:12px;border:0;border-radius:9px;background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;font-weight:900;cursor:pointer;font-size:15px}
.err{background:#3a1118;color:#ff7b8a;padding:9px;border-radius:7px;margin-bottom:10px;text-align:center;font-size:13px}
.back{text-align:center;margin-top:16px;font-size:12px}<a{color:#20e6c3}
</style>
</head>
<body>
<form class="box" method="post" action="/login">
<div class="logo">A</div>
<h1>ALPHA RADAR SIGNALS</h1>
<p>Admin Dashboard Login</p>
__ERR__
<input name="username" placeholder="Username" required autocomplete="username">
<input name="password" type="password" placeholder="Password" required autocomplete="current-password">
<button type="submit">LOGIN</button>
<div class="back"><a href="/" style="color:#8fa8c7;text-decoration:none">← Back to public site</a></div>
</form>
</body>
</html>
"""

_PUBLIC_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ALPHA RADAR SIGNALS &#8212; AI-Powered Binance Futures Signals</title>
<meta name="description" content="Free AI-powered Binance Futures signals. Multi-timeframe analysis. Risk managed entries. 24/7 market scanner."/>
<meta property="og:title" content="ALPHA RADAR SIGNALS &#8212; AI-Powered Futures Signals"/>
<meta property="og:description" content="Multi-Timeframe Analysis &#183; Risk Managed &#183; 24/7 Scanner &#183; Free on Telegram"/>
<meta property="og:type" content="website"/>
<meta name="twitter:card" content="summary_large_image"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet" crossorigin/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
:root{--bg:#040d1a;--surf:rgba(6,16,34,0.92);--card:rgba(7,18,40,0.88);--bdr:rgba(0,220,190,0.12);--bdr-h:rgba(0,220,190,0.32);--teal:#00f5d4;--green:#00ff7f;--red:#ff3d5a;--yellow:#ffd84d;--blue:#1a8cff;--text:#dce9f8;--sub:#8ab0cc;--muted:#4e6a87;--glow:rgba(0,245,212,0.1);--glows:rgba(0,245,212,0.25);--r:15px}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Inter',Arial,sans-serif;line-height:1.6;overflow-x:hidden;-webkit-font-smoothing:antialiased}
a{color:var(--teal);text-decoration:none}
button{font-family:inherit;cursor:pointer;border:none;background:none}
.container{max-width:1200px;margin:0 auto;padding:0 22px}
.section{padding:60px 0}
.section-sm{padding:40px 0}
.card{background:var(--card);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:1px solid var(--bdr);border-radius:var(--r);transition:border-color .25s,box-shadow .25s}
.card:hover{border-color:var(--bdr-h);box-shadow:0 0 28px var(--glow)}

/* NAV */
nav{position:sticky;top:0;z-index:100;background:rgba(4,13,26,0.9);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border-bottom:1px solid var(--bdr)}
.nav-in{display:flex;align-items:center;justify-content:space-between;padding:13px 22px;max-width:1200px;margin:0 auto;gap:14px}
.nav-logo{display:flex;align-items:center;gap:11px;flex-shrink:0}
.logo-mark{width:38px;height:38px;border:2px solid var(--teal);border-radius:50%;display:flex;align-items:center;justify-content:center;color:var(--teal);font-weight:900;font-size:17px;box-shadow:0 0 16px var(--glow)}
.logo-txt{font-size:14px;font-weight:900;letter-spacing:.5px}
.logo-txt em{color:var(--teal);font-style:normal}
.live-pill{background:rgba(0,245,212,0.08);color:var(--teal);border:1px solid rgba(0,245,212,0.28);border-radius:4px;padding:3px 9px;font-weight:800;font-size:10px;letter-spacing:2px;animation:liveblink 2s infinite}
@keyframes liveblink{0%,100%{opacity:1}50%{opacity:.55}}
.nav-links{display:flex;align-items:center;gap:18px}
.nav-links a{color:var(--sub);font-size:13px;font-weight:600;transition:color .2s}
.nav-links a:hover{color:var(--teal)}
.nav-right{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.nav-tg{background:rgba(0,136,204,0.15);border:1px solid rgba(0,136,204,0.35);color:#5bb7e3;padding:6px 13px;border-radius:7px;font-size:13px;font-weight:700;transition:all .2s}
.nav-tg:hover{background:rgba(0,136,204,0.25);color:#7dcef5}
.nav-dc{background:rgba(88,101,242,0.15);border:1px solid rgba(88,101,242,0.35);color:#8b95f7;padding:6px 13px;border-radius:7px;font-size:13px;font-weight:700;transition:all .2s}
.nav-dc:hover{background:rgba(88,101,242,0.25)}
.nav-admin{color:var(--muted);font-size:11px;padding:5px 10px;border:1px solid rgba(255,255,255,0.07);border-radius:6px;transition:all .2s}
.nav-admin:hover{border-color:var(--bdr-h);color:var(--sub)}

/* HERO */
.hero{padding:80px 0 56px;background:radial-gradient(ellipse 80% 45% at 15% 55%,rgba(0,245,212,0.05),transparent 60%),radial-gradient(ellipse 60% 45% at 85% 50%,rgba(26,140,255,0.04),transparent 60%)}
.hero-in{display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center}
.hero-eyebrow{display:inline-flex;align-items:center;gap:7px;background:rgba(0,245,212,0.07);border:1px solid rgba(0,245,212,0.22);border-radius:100px;padding:5px 16px;font-size:11px;letter-spacing:1px;color:var(--teal);font-weight:700;margin-bottom:18px}
.eye-dot{width:6px;height:6px;border-radius:50%;background:var(--teal);box-shadow:0 0 8px var(--teal);animation:liveblink 1.5s infinite}
.hero-h1{font-size:52px;font-weight:900;line-height:1.06;letter-spacing:-1px;margin-bottom:18px}
.h1-l1{display:block;color:var(--text)}
.h1-l2{display:block;color:var(--teal)}
.hero-p{font-size:16px;color:var(--sub);margin-bottom:26px;max-width:420px;line-height:1.7}
.hero-feats{display:flex;flex-direction:column;gap:9px;margin-bottom:34px}
.feat{display:flex;align-items:center;gap:10px;font-size:14px;color:var(--sub)}
.feat-chk{width:18px;height:18px;border-radius:50%;background:rgba(0,245,212,0.1);border:1px solid rgba(0,245,212,0.3);display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--teal);flex-shrink:0}
.hero-btns{display:flex;gap:12px;flex-wrap:wrap}
.btn-primary{background:linear-gradient(135deg,#00c9a7,#00f5d4);color:#020f18;padding:13px 26px;border-radius:10px;font-weight:800;font-size:14px;letter-spacing:.3px;transition:transform .2s,box-shadow .2s;display:inline-flex;align-items:center;gap:8px}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(0,245,212,0.3);color:#020f18}
.btn-outline{background:rgba(0,245,212,0.05);color:var(--teal);padding:13px 26px;border-radius:10px;font-weight:700;font-size:14px;border:1.5px solid rgba(0,245,212,0.28);transition:all .2s;display:inline-flex;align-items:center;gap:8px}
.btn-outline:hover{background:rgba(0,245,212,0.1);border-color:var(--teal);transform:translateY(-2px)}

/* RADAR */
.radar-wrap{position:relative;width:420px;height:420px;max-width:100%;margin:0 auto}
.radar-bg{position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle,rgba(0,245,212,0.04),transparent 70%)}
.rring{position:absolute;border-radius:50%;border:1px solid;top:50%;left:50%;transform:translate(-50%,-50%)}
.rr1{width:100%;height:100%;border-color:rgba(0,245,212,0.1)}
.rr2{width:67%;height:67%;border-color:rgba(0,245,212,0.17)}
.rr3{width:33%;height:33%;border-color:rgba(0,245,212,0.26)}
.radar-sweep{position:absolute;inset:0;border-radius:50%;background:conic-gradient(from 0deg,transparent 300deg,rgba(0,245,212,0.06) 335deg,rgba(0,245,212,0.22) 360deg);animation:rsweep 3s linear infinite}
@keyframes rsweep{to{transform:rotate(360deg)}}
.radar-cx{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:60px;height:60px;border-radius:50%;background:radial-gradient(circle,rgba(0,245,212,0.18),transparent 70%);border:2px solid var(--teal);display:flex;align-items:center;justify-content:center;font-size:21px;font-weight:900;color:var(--teal);box-shadow:0 0 30px rgba(0,245,212,0.38),0 0 60px rgba(0,245,212,0.1);z-index:2}
.rdot{position:absolute;width:6px;height:6px;border-radius:50%;background:var(--teal);box-shadow:0 0 10px var(--teal);animation:rdotblink 2s ease-in-out infinite}
@keyframes rdotblink{0%,100%{opacity:.9;transform:scale(1)}50%{opacity:.3;transform:scale(.6)}}
.rd1{top:22%;left:68%;animation-delay:.3s}
.rd2{top:64%;left:73%;animation-delay:.9s}
.rd3{top:76%;left:37%;animation-delay:1.5s}
.rd4{top:28%;left:26%;animation-delay:2.1s}
.fchip{position:absolute;background:rgba(4,13,26,0.88);backdrop-filter:blur(10px);border:1px solid rgba(0,245,212,0.28);border-radius:9px;padding:7px 14px;font-size:12px;font-weight:700;color:var(--teal);white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,0.4)}
.fc-btc{top:6%;left:54%;animation:fc1 4s ease-in-out infinite}
.fc-eth{top:54%;left:76%;animation:fc2 4s ease-in-out infinite .8s}
.fc-sol{top:80%;left:14%;animation:fc3 4s ease-in-out infinite 1.6s}
@keyframes fc1{0%,100%{transform:translateY(0)}50%{transform:translateY(-11px)}}
@keyframes fc2{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
@keyframes fc3{0%,100%{transform:translateY(0)}50%{transform:translateY(-13px)}}

/* STATS BAR */
.stats-bar{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin:0 0 14px}
.scard{padding:20px;text-align:center}
.sc-lbl{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.sc-val{font-size:28px;font-weight:900;line-height:1}
.sc-hint{font-size:10px;color:var(--muted);margin-top:5px}
.ct{color:var(--teal)}.cg{color:var(--green)}.cr{color:var(--red)}.cy{color:var(--yellow)}.cb{color:var(--blue)}

/* SECTION HEADER */
.sh{margin-bottom:28px}
.sh-lbl{display:inline-flex;align-items:center;gap:6px;background:rgba(0,245,212,0.07);border:1px solid rgba(0,245,212,0.18);border-radius:5px;padding:4px 12px;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--teal);font-weight:700;margin-bottom:10px}
.sh-title{font-size:26px;font-weight:900;color:var(--text)}
.sh-sub{font-size:14px;color:var(--sub);margin-top:6px}

/* SIGNAL TABLE */
.stbl{width:100%;border-collapse:collapse}
.stbl th{text-align:left;padding:10px 13px;font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);border-bottom:1px solid rgba(255,255,255,0.05)}
.stbl td{padding:11px 13px;font-size:13px;border-bottom:1px solid rgba(255,255,255,0.04)}
.stbl tr:last-child td{border-bottom:none}
.stbl tr:hover td{background:rgba(0,245,212,0.02)}
.bl{background:rgba(0,255,127,0.1);color:var(--green);border:1px solid rgba(0,255,127,0.22);padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}
.bs{background:rgba(255,61,90,0.1);color:var(--red);border:1px solid rgba(255,61,90,0.22);padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}
.bopen{color:var(--teal);font-weight:700}
.btp{color:var(--green);font-weight:700}
.bsl{color:var(--red);font-weight:700}
.bexp{color:var(--yellow);font-weight:700}
.ovx{overflow-x:auto;-webkit-overflow-scrolling:touch}

/* PERF */
.perf-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin-bottom:20px}
.pcrd{padding:22px;text-align:center}
.plbl{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.pval{font-size:34px;font-weight:900;line-height:1}
.phint{font-size:11px;color:var(--muted);margin-top:6px}

/* TABS */
.tabs{display:flex;gap:7px;margin-bottom:14px;padding:0 0 0 2px}
.tbtn{padding:7px 15px;border-radius:7px;border:1px solid var(--bdr);color:var(--muted);font-size:12px;font-weight:600;transition:all .2s}
.tbtn.on{background:rgba(0,245,212,0.07);border-color:rgba(0,245,212,0.28);color:var(--teal)}
.tpn{display:none}.tpn.on{display:block}

/* EXCHANGE CARDS */
.exch-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.exch-card{padding:24px;border-radius:16px;text-align:center;transition:transform .25s,box-shadow .25s}
.exch-card:hover{transform:translateY(-5px);box-shadow:0 12px 36px rgba(0,0,0,0.5)}
.exch-ico{width:54px;height:54px;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:900;margin:0 auto 14px;border:2px solid}
.exch-name{font-size:17px;font-weight:900;margin-bottom:6px}
.exch-desc{font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.5}
.exch-btn{display:block;padding:10px;border-radius:8px;font-weight:700;font-size:13px;transition:all .2s;background:rgba(0,245,212,0.07);border:1px solid rgba(0,245,212,0.25);color:var(--teal)}
.exch-btn:hover{background:rgba(0,245,212,0.14);border-color:var(--teal);color:var(--teal)}

/* TELEGRAM CTA */
.tg-cta{background:linear-gradient(135deg,rgba(0,136,204,0.07),rgba(0,245,212,0.05));border:1px solid rgba(0,136,204,0.18);border-radius:22px;padding:56px 40px;text-align:center;position:relative;overflow:hidden}
.tg-cta::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 70% 60% at 50% 50%,rgba(0,136,204,0.05),transparent 70%);pointer-events:none}
.tg-title{font-size:36px;font-weight:900;margin-bottom:10px}
.tg-sub{font-size:15px;color:var(--sub);max-width:480px;margin:0 auto 32px}
.tg-bens{display:flex;flex-wrap:wrap;justify-content:center;gap:10px 22px;margin-bottom:32px}
.tg-ben{display:flex;align-items:center;gap:7px;font-size:14px;color:var(--sub)}
.ben-dot{width:6px;height:6px;border-radius:50%;background:var(--teal);flex-shrink:0}
.btn-tg{display:inline-flex;align-items:center;gap:9px;background:#0088cc;color:#fff;padding:15px 34px;border-radius:11px;font-weight:800;font-size:16px;transition:all .22s;box-shadow:0 8px 24px rgba(0,136,204,0.28)}
.btn-tg:hover{background:#009ee0;transform:translateY(-2px);box-shadow:0 12px 32px rgba(0,136,204,0.42);color:#fff}

/* DONATIONS */
.don-grid{display:grid;grid-template-columns:1fr 1fr;gap:15px}
.don-card{padding:20px}
.don-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.don-coin{font-size:14px;font-weight:900;letter-spacing:.5px}
.don-net{font-size:10px;color:var(--muted);background:rgba(255,255,255,0.05);padding:3px 9px;border-radius:4px}
.don-addr{font-family:monospace,monospace;font-size:11px;color:var(--teal);background:rgba(0,0,0,0.28);border:1px solid var(--bdr);border-radius:8px;padding:9px;word-break:break-all;line-height:1.5;margin-bottom:10px;transition:border-color .2s;cursor:default}
.don-addr:hover{border-color:rgba(0,245,212,0.3)}
.don-acts{display:flex;gap:7px}
.don-btn{flex:1;padding:8px;border-radius:7px;font-size:12px;font-weight:700;cursor:pointer;transition:all .2s;border:1px solid}
.don-copy{background:rgba(0,245,212,0.07);border-color:rgba(0,245,212,0.22);color:var(--teal)}
.don-copy:hover{background:rgba(0,245,212,0.14);border-color:var(--teal)}
.don-qr{background:rgba(255,255,255,0.04);border-color:var(--bdr);color:var(--sub)}
.don-qr:hover{border-color:var(--bdr-h);color:var(--text)}

/* FAQ */
.faq-item{margin-bottom:7px}
.faq-q{padding:15px 18px;font-size:14px;font-weight:700;cursor:pointer;display:flex;justify-content:space-between;align-items:center;border-radius:var(--r)}
.faq-q:hover{background:rgba(0,245,212,0.03)}
.faq-arr{color:var(--muted);transition:transform .2s;font-size:11px}
.faq-a{font-size:13px;color:var(--sub);line-height:1.7;max-height:0;overflow:hidden;padding:0 18px;transition:max-height .3s ease,padding .3s}
.faq-item.open .faq-a{max-height:200px;padding:0 18px 15px}
.faq-item.open .faq-arr{transform:rotate(180deg)}

/* LEADERBOARD */
.lbrow{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.04)}
.lbrow:last-child{border-bottom:none}
.lbrank{font-size:14px;font-weight:900;width:26px;color:var(--muted)}
.lbsym{font-size:13px;font-weight:700;flex:1}
.lbr{text-align:right}
.lbpnl{font-size:13px;font-weight:800}
.lbcnt{font-size:10px;color:var(--muted);margin-top:2px}

/* DISC */
.disc{background:rgba(255,61,90,0.04);border:1px solid rgba(255,61,90,0.1);border-radius:13px;padding:18px;margin-top:36px}
.disc h4{color:rgba(255,110,130,0.9);font-size:12px;margin-bottom:7px;display:flex;align-items:center;gap:6px}
.disc p{font-size:12px;color:rgba(170,100,110,0.9);line-height:1.7}

/* FOOTER */
footer{border-top:1px solid var(--bdr);padding:48px 0 32px;margin-top:40px}
.footer-in{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr;gap:32px;margin-bottom:36px}
.fbrand{font-size:14px;font-weight:900;margin-bottom:7px}
.ftagline{font-size:12px;color:var(--muted);line-height:1.7}
.fcol-ttl{font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:14px}
.flinks{display:flex;flex-direction:column;gap:8px}
.flinks a{font-size:13px;color:var(--sub);transition:color .2s}
.flinks a:hover{color:var(--teal)}
.fbot{border-top:1px solid var(--bdr);padding-top:20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
.fcopy{font-size:11px;color:var(--muted)}

/* FLOAT TG */
.ftg{position:fixed;bottom:22px;right:22px;width:54px;height:54px;background:#0088cc;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 8px 22px rgba(0,136,204,0.42);z-index:90;transition:all .2s}
.ftg:hover{transform:scale(1.1);box-shadow:0 12px 30px rgba(0,136,204,0.58)}
.ftg svg{width:26px;height:26px;fill:#fff}

/* TOAST */
.toast{position:fixed;bottom:88px;right:22px;background:rgba(0,245,212,0.1);backdrop-filter:blur(14px);border:1px solid rgba(0,245,212,0.28);color:var(--teal);padding:9px 16px;border-radius:8px;font-size:13px;font-weight:700;z-index:200;opacity:0;transition:opacity .25s;pointer-events:none}
.toast.show{opacity:1}

/* QR MODAL */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,0.76);backdrop-filter:blur(6px);z-index:300;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .2s}
.modal-bg.open{opacity:1;pointer-events:all}
.modal-box{background:rgba(5,14,28,0.97);border:1px solid var(--bdr-h);border-radius:20px;padding:30px;text-align:center;max-width:300px;width:90%}
.modal-ttl{font-size:15px;font-weight:900;margin-bottom:3px}
.modal-net{font-size:11px;color:var(--muted);margin-bottom:18px}
.modal-qr{background:#fff;padding:10px;border-radius:10px;display:inline-block;margin-bottom:14px}
.modal-addr{font-family:monospace;font-size:10px;color:var(--teal);word-break:break-all;background:rgba(0,0,0,0.3);border:1px solid var(--bdr);border-radius:7px;padding:8px;margin-bottom:14px}
.modal-close{background:rgba(255,255,255,0.06);border:1px solid var(--bdr);color:var(--sub);padding:8px 22px;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer}
.modal-close:hover{border-color:var(--bdr-h);color:var(--text)}

/* RESPONSIVE */
@media(max-width:1020px){.stats-bar{grid-template-columns:repeat(3,1fr)}.perf-grid{grid-template-columns:1fr 1fr}.footer-in{grid-template-columns:1fr 1fr}.exch-grid{grid-template-columns:1fr 1fr}}
@media(max-width:768px){.hero{padding:56px 0 36px}.hero-in{grid-template-columns:1fr;gap:30px}.hero-h1{font-size:36px}.hero-right{order:-1}.radar-wrap{width:290px;height:290px}.stats-bar{grid-template-columns:1fr 1fr}.don-grid{grid-template-columns:1fr}.tg-cta{padding:38px 22px}.tg-title{font-size:26px}.nav-links{display:none}}
@media(max-width:480px){.stats-bar{grid-template-columns:1fr}.exch-grid{grid-template-columns:1fr}.hero-h1{font-size:30px}.footer-in{grid-template-columns:1fr}.perf-grid{grid-template-columns:1fr}.fbot{flex-direction:column;text-align:center}}
</style>
</head>
<body>

<nav>
<div class="nav-in">
  <div class="nav-logo">
    <div class="logo-mark">A</div>
    <div class="logo-txt">ALPHA RADAR <em>SIGNALS</em></div>
  </div>
  <div class="nav-links">
    <a href="#live-stats">Stats</a>
    <a href="#signals-section">Signals</a>
    <a href="#perf-section">Performance</a>
    <a href="#exchanges-section">Exchanges</a>
    <a href="/faq">FAQ</a>
  </div>
  <div class="nav-right">
    <span class="live-pill">&#9679; LIVE</span>
    __TG_BTN__
    __DC_BTN__
    <a href="/admin" class="nav-admin">Admin</a>
  </div>
</div>
</nav>

<div class="hero">
<div class="container">
<div class="hero-in">
  <div>
    <div class="hero-eyebrow"><span class="eye-dot"></span>AI-Powered Binance Futures</div>
    <h1 class="hero-h1"><span class="h1-l1">AI-POWERED</span><span class="h1-l2">FUTURES SIGNALS</span></h1>
    <p class="hero-p">Professional-grade crypto futures signals powered by multi-timeframe AI analysis. Free. No subscription required.</p>
    <div class="hero-feats">
      <div class="feat"><div class="feat-chk">&#10003;</div>Multi-Timeframe Analysis &mdash; 1D / 4H / 1H / 15M</div>
      <div class="feat"><div class="feat-chk">&#10003;</div>Risk Managed Entries &mdash; SL &amp; TP defined</div>
      <div class="feat"><div class="feat-chk">&#10003;</div>24/7 Market Scanner &mdash; 200+ USDT pairs</div>
      <div class="feat"><div class="feat-chk">&#10003;</div>Live Performance Tracking</div>
    </div>
    <div class="hero-btns">__HERO_BTNS__</div>
  </div>
  <div class="hero-right" style="display:flex;justify-content:center">
    <div class="radar-wrap">
      <div class="radar-bg"></div>
      <div class="rring rr1"></div>
      <div class="rring rr2"></div>
      <div class="rring rr3"></div>
      <div class="radar-sweep"></div>
      <div class="radar-cx">A</div>
      <div class="rdot rd1"></div>
      <div class="rdot rd2"></div>
      <div class="rdot rd3"></div>
      <div class="rdot rd4"></div>
      <div class="fchip fc-btc" id="chip-btc">&#8383; BTCUSDT</div>
      <div class="fchip fc-eth" id="chip-eth">&#9841; ETHUSDT</div>
      <div class="fchip fc-sol" id="chip-sol">&#9788; SOLUSDT</div>
    </div>
  </div>
</div>
</div>
</div>

<div id="live-stats" class="container section-sm">
<div class="stats-bar">
  <div class="scard card"><div class="sc-lbl">Total Signals (30D)</div><div id="s-total" class="sc-val ct">&#8212;</div><div class="sc-hint">MTF pipeline</div></div>
  <div class="scard card"><div class="sc-lbl">Win Rate (30D)</div><div id="s-wr" class="sc-val cg">&#8212;</div><div class="sc-hint">Closed trades</div></div>
  <div class="scard card"><div class="sc-lbl">Avg Risk / Reward</div><div id="s-rr" class="sc-val cy">&#8212;</div><div class="sc-hint">1:X ratio</div></div>
  <div class="scard card"><div class="sc-lbl">Markets Scanned</div><div id="s-mkts" class="sc-val cb">&#8212;</div><div class="sc-hint">USDT pairs</div></div>
  <div class="scard card"><div class="sc-lbl">Open Positions</div><div id="s-active" class="sc-val ct">&#8212;</div><div class="sc-hint">Live now</div></div>
</div>
</div>

<div id="signals-section" class="container section-sm">
<div class="sh">
  <div class="sh-lbl">&#9679; REAL-TIME</div>
  <div class="sh-title">Live Signals</div>
  <div class="sh-sub">Latest AI-generated trade setups from the multi-timeframe pipeline</div>
</div>
<div class="card" style="padding:4px">
<div class="ovx">
<table class="stbl">
<thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
<tbody id="sig-tbl"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:32px">Loading signals...</td></tr></tbody>
</table>
</div>
</div>
</div>

<div id="perf-section" class="container section-sm">
<div class="sh">
  <div class="sh-lbl">&#128200; PERFORMANCE</div>
  <div class="sh-title">Track Record</div>
  <div class="sh-sub">Live performance metrics from all closed trades</div>
</div>
<div class="perf-grid">
  <div class="pcrd card"><div class="plbl">Win Rate</div><div id="ps-wr" class="pval cg">&#8212;</div><div class="phint"><span id="ps-w" class="cg">&#8212;</span> W &nbsp;/&nbsp; <span id="ps-l" class="cr">&#8212;</span> L</div></div>
  <div class="pcrd card"><div class="plbl">Profit Factor</div><div id="ps-pf" class="pval ct">&#8212;</div><div class="phint">All closed trades</div></div>
  <div class="pcrd card"><div class="plbl">Avg PnL / Trade</div><div id="ps-pnl" class="pval cg">&#8212;</div><div class="phint">Closed only</div></div>
  <div class="pcrd card"><div class="plbl">Open Now</div><div id="ps-open" class="pval ct">&#8212;</div><div class="phint">Active positions</div></div>
</div>
<div class="card" style="padding:22px;margin-bottom:18px">
  <div style="font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:14px">Equity Curve</div>
  <canvas id="equity-chart" height="160"></canvas>
  <div id="equity-empty" style="text-align:center;color:var(--muted);padding:36px;display:none">Not enough data yet</div>
</div>
<div class="card" style="padding:18px">
<div class="tabs">
  <button class="tbtn on" onclick="swTab('open',this)">Open (<span id="tc-open">&#8212;</span>)</button>
  <button class="tbtn" onclick="swTab('closed',this)">Closed</button>
</div>
<div id="tp-open" class="tpn on ovx">
<table class="stbl"><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>ENTRY</th><th>TP1</th><th>SL</th><th>CONF</th><th>RR</th></tr></thead>
<tbody id="open-tbl"><tr><td colspan="9" style="text-align:center;color:var(--muted);padding:20px">No open signals</td></tr></tbody>
</table>
</div>
<div id="tp-closed" class="tpn ovx">
<table class="stbl"><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>RESULT</th><th>PNL</th></tr></thead>
<tbody id="closed-tbl"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:20px">Loading...</td></tr></tbody>
</table>
</div>
</div>
<div class="card" style="padding:18px;margin-top:16px">
<div style="font-size:11px;font-weight:700;color:var(--muted);margin-bottom:12px;letter-spacing:1px">PERFORMANCE LEADERBOARD</div>
<div id="lb-list"><p style="color:var(--muted);text-align:center;padding:20px">Loading...</p></div>
</div>
</div>

<div id="exchanges-section" class="container section">
__AFFILIATES__
</div>

<div class="container" style="margin-bottom:40px">
<div class="tg-cta">
  <div class="sh-lbl" style="justify-content:center;margin:0 auto 14px;display:inline-flex">&#128241; JOIN FREE</div>
  <div class="tg-title">Join <span style="color:var(--teal)">Thousands</span> of Traders</div>
  <div class="tg-sub">Real-time signals, market alerts, weekly performance reports &mdash; all free on Telegram.</div>
  <div class="tg-bens">
    <div class="tg-ben"><div class="ben-dot"></div>Real-time signal alerts</div>
    <div class="tg-ben"><div class="ben-dot"></div>Market regime updates</div>
    <div class="tg-ben"><div class="ben-dot"></div>Weekly performance reports</div>
    <div class="tg-ben"><div class="ben-dot"></div>24/7 scanner coverage</div>
    <div class="tg-ben"><div class="ben-dot"></div>100% free to join</div>
  </div>
  <a id="cta-tg-btn" href="__TG_URL__" target="_blank" rel="noopener" class="btn-tg">
    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.96 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>
    JOIN TELEGRAM FREE
  </a>
</div>
</div>

<div class="container section-sm">
__DONATE__
</div>

<div class="container section-sm">
<div class="sh">
  <div class="sh-lbl">&#10067; FAQ</div>
  <div class="sh-title">Frequently Asked Questions</div>
</div>
<div id="faq-list">
  <div class="faq-item card" onclick="toggleFaq(this)"><div class="faq-q">What is Alpha Radar Signals?<span class="faq-arr">&#9660;</span></div><div class="faq-a">A free AI-powered crypto futures signal service. Our multi-timeframe engine scans 200+ USDT perpetual pairs on Binance 24/7, applying a strict 4-layer pipeline (1D Trend &rarr; 4H Structure &rarr; 1H Setup &rarr; 15M Entry) to deliver high-quality setups directly to Telegram.</div></div>
  <div class="faq-item card" onclick="toggleFaq(this)" style="margin-top:7px"><div class="faq-q">Is this financial advice?<span class="faq-arr">&#9660;</span></div><div class="faq-a">No. All signals are for educational and informational purposes only. Nothing on this platform constitutes financial, investment, trading, or legal advice. You are solely responsible for your trading decisions.</div></div>
  <div class="faq-item card" onclick="toggleFaq(this)" style="margin-top:7px"><div class="faq-q">Does the bot trade automatically?<span class="faq-arr">&#9660;</span></div><div class="faq-a">No. Alpha Radar Signals does not place any real trades. All signals require manual execution by the user. The system only generates and broadcasts trade setups &mdash; it never connects to your exchange account or touches real funds.</div></div>
  <div class="faq-item card" onclick="toggleFaq(this)" style="margin-top:7px"><div class="faq-q">How are signals generated?<span class="faq-arr">&#9660;</span></div><div class="faq-a">Each signal must pass four hard gates: (1) 1D Trend Filter &mdash; EMA alignment confirms the dominant trend. (2) 4H Structure &mdash; BOS, OB, FVG confirm momentum. (3) 1H Setup &mdash; pullback, VWAP, volume confirm entry zone. (4) 15M Entry trigger fires on BOS, FVG retest, OB, EMA pullback, or VWAP reclaim.</div></div>
  <div class="faq-item card" onclick="toggleFaq(this)" style="margin-top:7px"><div class="faq-q">What exchanges are supported?<span class="faq-arr">&#9660;</span></div><div class="faq-a">Signals are calibrated for Binance USDT Perpetual Futures. The setups are also compatible with Bybit, OKX, and Bitget for the same pairs.</div></div>
</div>
</div>

<div class="container">
<div class="disc">
  <h4>&#9888; Risk Disclaimer</h4>
  <p>Signals are for educational purposes only. Trading futures is high risk. Past performance does not indicate future results. Never trade with money you cannot afford to lose. Alpha Radar Signals does not provide financial, investment, or legal advice. <a href="/risk-disclaimer" style="color:rgba(255,110,130,0.8)">Full disclaimer &rarr;</a></p>
</div>
</div>

<footer>
<div class="container">
<div class="footer-in">
  <div>
    <div class="fbrand">ALPHA RADAR <span style="color:var(--teal)">SIGNALS</span></div>
    <div class="ftagline">AI-Powered Binance Futures Signals.<br/>Multi-Timeframe &middot; Risk Managed &middot; 24/7 Scanner.<br/>For educational use only.</div>
  </div>
  <div>
    <div class="fcol-ttl">Platform</div>
    <div class="flinks">
      <a href="/signals">Signals</a>
      <a href="/performance">Performance</a>
      <a href="/stats">Stats</a>
      <a href="/about">About</a>
      <a href="/faq">FAQ</a>
    </div>
  </div>
  <div>
    <div class="fcol-ttl">Community</div>
    <div class="flinks" id="footer-community">__FOOTER_COMM__</div>
  </div>
  <div>
    <div class="fcol-ttl">Legal</div>
    <div class="flinks">
      <a href="/terms">Terms of Service</a>
      <a href="/privacy">Privacy Policy</a>
      <a href="/risk-disclaimer">Risk Disclaimer</a>
      <a href="/admin">Admin</a>
    </div>
  </div>
</div>
<div class="fbot">
  <div class="fcopy">&copy; 2026 ALPHA RADAR SIGNALS &middot; Not financial advice. For educational use only.</div>
  <div class="fcopy"><a href="/signals" style="color:var(--muted)">Signals</a> &middot; <a href="/performance" style="color:var(--muted)">Performance</a> &middot; <a href="/stats" style="color:var(--muted)">Stats</a></div>
</div>
</div>
</footer>

<a id="float-tg" href="__TG_URL__" target="_blank" rel="noopener" class="ftg" title="Join Telegram">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.96 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>
</a>

<div id="qr-modal" class="modal-bg" onclick="closeQR(event)">
  <div class="modal-box">
    <div id="qr-ttl" class="modal-ttl">&#8212;</div>
    <div id="qr-net" class="modal-net">&#8212;</div>
    <div class="modal-qr"><div id="qr-canvas"></div></div>
    <div id="qr-addr" class="modal-addr">&#8212;</div>
    <button class="modal-close" onclick="closeQRBtn()">Close</button>
  </div>
</div>
<div id="v4-toast" class="toast">Copied!</div>

<script>
function pct(v){if(v===null||v===undefined)return'&#8212;';var n=parseFloat(v);return(n>=0?'+':'')+n.toFixed(2)+'%';}
function cls(v){return parseFloat(v)>=0?'cg':'cr';}
function sBadge(st){
  if(st==='OPEN')return'<span class="bopen">OPEN</span>';
  if(st==='SL')return'<span class="bsl">SL</span>';
  if(st==='EXPIRED')return'<span class="bexp">EXP</span>';
  return'<span class="btp">'+st+'</span>';
}
function swTab(n,btn){
  document.querySelectorAll('.tbtn').forEach(function(b){b.classList.remove('on');});
  btn.classList.add('on');
  document.querySelectorAll('.tpn').forEach(function(p){p.classList.remove('on');});
  document.getElementById('tp-'+n).classList.add('on');
}
function toggleFaq(el){el.classList.toggle('open');}
function showToast(msg){
  var t=document.getElementById('v4-toast');
  t.textContent=msg||'Copied!';
  t.classList.add('show');
  setTimeout(function(){t.classList.remove('show');},2000);
}
function copyDonAddr(btn,addr){
  if(navigator.clipboard){
    navigator.clipboard.writeText(addr).then(function(){
      showToast('Address copied!');
      var orig=btn.textContent;btn.textContent='Copied!';
      setTimeout(function(){btn.textContent=orig;},1800);
    }).catch(function(){});
  }
}
var _qrObj=null;
function showQR(label,network,addr){
  document.getElementById('qr-ttl').textContent=label;
  document.getElementById('qr-net').textContent=network;
  document.getElementById('qr-addr').textContent=addr;
  var c=document.getElementById('qr-canvas');
  c.innerHTML='';
  if(window.QRCode){
    try{_qrObj=new QRCode(c,{text:addr,width:170,height:170,colorDark:'#000000',colorLight:'#ffffff'});}
    catch(e){c.textContent='QR unavailable';}
  }else{c.textContent='QR library loading...';}
  document.getElementById('qr-modal').classList.add('open');
}
function closeQR(e){if(e.target===document.getElementById('qr-modal'))closeQRBtn();}
function closeQRBtn(){document.getElementById('qr-modal').classList.remove('open');}

var _equityChart=null;
function buildEquity(closed){
  var canvas=document.getElementById('equity-chart');
  var empty=document.getElementById('equity-empty');
  if(!closed||closed.length<2){empty.style.display='block';canvas.style.display='none';return;}
  empty.style.display='none';canvas.style.display='block';
  var cum=0,labels=[],data=[];
  closed.slice().reverse().forEach(function(s,i){
    cum+=parseFloat(s.pnl||0);
    labels.push(s.time||(''+(i+1)));
    data.push(Math.round(cum*100)/100);
  });
  var ctx=canvas.getContext('2d');
  if(_equityChart)_equityChart.destroy();
  var grad=ctx.createLinearGradient(0,0,0,160);
  grad.addColorStop(0,'rgba(0,245,212,0.22)');
  grad.addColorStop(1,'rgba(0,245,212,0.01)');
  _equityChart=new Chart(ctx,{
    type:'line',
    data:{labels:labels,datasets:[{data:data,borderColor:'#00f5d4',borderWidth:2,backgroundColor:grad,fill:true,tension:0.4,pointRadius:0,pointHoverRadius:4,pointHoverBackgroundColor:'#00f5d4'}]},
    options:{responsive:true,plugins:{legend:{display:false},tooltip:{callbacks:{label:function(c){return(c.raw>=0?'+':'')+c.raw+'%';},title:function(c){return c[0].label;}}}},scales:{x:{display:false},y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'#4e6a87',font:{size:10},callback:function(v){return(v>=0?'+':'')+v+'%';}}},}},
  });
}

async function loadStats(){
  try{
    var r=await fetch('/api/public/stats');
    if(!r.ok)return;
    var d=await r.json();
    if(d.error)return;
    var wrEl=document.getElementById('s-wr');
    wrEl.textContent=(d.winrate!=null?d.winrate:'&#8212;')+'%';
    wrEl.className='sc-val '+(d.winrate>=50?'cg':'cr');
    document.getElementById('s-active').textContent=d.open_signals!=null?d.open_signals:'&#8212;';
    document.getElementById('s-mkts').textContent=d.universe!=null?d.universe:'&#8212;';
    var pwEl=document.getElementById('ps-wr');
    pwEl.textContent=(d.winrate!=null?d.winrate:'&#8212;')+'%';
    pwEl.className='pval '+(d.winrate>=50?'cg':'cr');
    document.getElementById('ps-open').textContent=d.open_signals!=null?d.open_signals:'&#8212;';
    document.getElementById('tc-open').textContent=d.open_signals!=null?d.open_signals:'&#8212;';
    document.getElementById('ps-w').textContent=d.wins!=null?d.wins:'&#8212;';
    document.getElementById('ps-l').textContent=d.losses!=null?d.losses:'&#8212;';
    var sRows=(d.recent||[]).slice(0,10).map(function(x){
      return'<tr><td style="color:var(--muted);font-size:12px">'+x.time+'</td>'+
        '<td><b><a href="/signal/'+x.id+'" style="color:var(--teal)">'+x.symbol+'</a></b></td>'+
        '<td><span class="'+(x.side==='LONG'?'bl':'bs')+'">'+x.side+'</span></td>'+
        '<td style="color:var(--sub)">'+x.tf+'</td>'+
        '<td style="color:var(--yellow)">'+x.conf+'%</td>'+
        '<td style="color:var(--sub)">1:'+x.rr+'</td>'+
        '<td>'+sBadge(x.status)+'</td>'+
        '<td class="'+cls(x.pnl)+'">'+pct(x.pnl)+'</td></tr>';
    }).join('');
    document.getElementById('sig-tbl').innerHTML=sRows||'<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:28px">No signals yet</td></tr>';
    var oRows=(d.open||[]).map(function(x){
      return'<tr><td style="font-size:12px;color:var(--muted)">'+x.time+'</td>'+
        '<td><b>'+x.symbol+'</b></td>'+
        '<td><span class="'+(x.side==='LONG'?'bl':'bs')+'">'+x.side+'</span></td>'+
        '<td style="color:var(--sub)">'+x.tf+'</td>'+
        '<td style="color:var(--teal)">'+x.entry_low+'</td>'+
        '<td style="color:var(--green)">'+x.tp1+'</td>'+
        '<td style="color:var(--red)">'+x.sl+'</td>'+
        '<td style="color:var(--yellow)">'+x.conf+'%</td>'+
        '<td style="color:var(--sub)">1:'+x.rr+'</td></tr>';
    }).join('');
    document.getElementById('open-tbl').innerHTML=oRows||'<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:20px">No open signals</td></tr>';
    var cRows=(d.closed_recent||[]).map(function(x){
      return'<tr><td style="font-size:12px;color:var(--muted)">'+x.time+'</td>'+
        '<td><b>'+x.symbol+'</b></td>'+
        '<td><span class="'+(x.side==='LONG'?'bl':'bs')+'">'+x.side+'</span></td>'+
        '<td style="color:var(--sub)">'+x.tf+'</td>'+
        '<td style="color:var(--yellow)">'+x.conf+'%</td>'+
        '<td style="color:var(--sub)">1:'+x.rr+'</td>'+
        '<td>'+sBadge(x.status)+'</td>'+
        '<td class="'+cls(x.pnl)+'">'+pct(x.pnl)+'</td></tr>';
    }).join('');
    document.getElementById('closed-tbl').innerHTML=cRows||'<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:20px">No closed signals</td></tr>';
    var lbHtml=(d.leaderboard||[]).map(function(x,i){
      var clr=i===0?'#ffd84d':i===1?'#b0b8c0':i===2?'#cd7f32':'var(--muted)';
      return'<div class="lbrow"><div class="lbrank" style="color:'+clr+'">#'+(i+1)+'</div>'+
        '<div class="lbsym">'+x.symbol+'</div>'+
        '<div class="lbr"><div class="lbpnl '+cls(x.avg)+'">'+pct(x.avg)+'</div>'+
        '<div class="lbcnt">'+x.count+' signals</div></div></div>';
    }).join('');
    document.getElementById('lb-list').innerHTML=lbHtml||'<p style="color:var(--muted);text-align:center;padding:20px">No data yet</p>';
    if(window.Chart)buildEquity(d.closed_recent||[]);
  }catch(e){console.error(e);}
}

async function loadPrices(){
  try{
    var r=await fetch('/api/public/prices');
    if(!r.ok)return;
    var d=await r.json();
    if(d.prices){
      var b=document.getElementById('chip-btc');
      var e=document.getElementById('chip-eth');
      var s=document.getElementById('chip-sol');
      if(b&&d.prices.BTCUSDT)b.textContent='&#8383; $'+Number(d.prices.BTCUSDT).toLocaleString();
      if(e&&d.prices.ETHUSDT)e.textContent='&#9841; $'+Number(d.prices.ETHUSDT).toLocaleString();
      if(s&&d.prices.SOLUSDT)s.textContent='&#9788; $'+Number(d.prices.SOLUSDT).toLocaleString();
    }
  }catch(e){}
}

async function loadPerf(){
  try{
    var r=await fetch('/api/public/performance');
    if(!r.ok)return;
    var d=await r.json();
    if(d.error)return;
    document.getElementById('s-total').textContent=d.total_signals!=null?d.total_signals:(d.total_closed!=null?d.total_closed:'&#8212;');
    document.getElementById('s-rr').textContent=d.avg_rr!=null?('1:'+d.avg_rr):'&#8212;';
    document.getElementById('ps-pf').textContent=d.profit_factor!=null?d.profit_factor:'&#8734;';
    var pEl=document.getElementById('ps-pnl');
    if(d.avg_pnl!=null){pEl.textContent=pct(d.avg_pnl);pEl.className='pval '+(d.avg_pnl>=0?'cg':'cr');}
  }catch(e){}
}

loadStats();loadPerf();loadPrices();
setInterval(loadStats,6000);
setInterval(loadPerf,30000);
setInterval(loadPrices,4000);
</script>
</body>
</html>
"""

_ADMIN_HTML = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Admin — ALPHA RADAR SIGNALS</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#070b12;color:#eaf2ff;font-family:Inter,Arial,sans-serif}
.wrap{display:grid;grid-template-columns:270px 1fr;min-height:100vh}
.side{background:linear-gradient(180deg,#08111c,#07101a);border-right:1px solid #13263a;padding:22px 18px}
.logo{display:flex;align-items:center;gap:12px;margin-bottom:32px}
.mark{width:56px;height:56px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:26px;box-shadow:0 0 22px #00ffc855}
.brand{font-size:19px;font-weight:900;line-height:1.1}.brand span{color:#20f0c0;letter-spacing:3px;font-size:11px;display:block}
.nav div{padding:12px;border-radius:9px;margin:6px 0;color:#bdd3ee;cursor:pointer;font-size:14px}
.nav div:hover{background:#0f2030}
.nav div.act{background:linear-gradient(90deg,#08a98f,#0d403b);color:#fff}
.status{position:absolute;bottom:24px;width:224px;border:1px solid #17314b;border-radius:12px;padding:16px;background:#0b1320;font-size:13px}.ok{color:#19ff82}
.main{padding:24px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}
h1{margin:0;color:#22e6c3;font-size:28px;letter-spacing:1px}h2{font-size:15px;margin:0 0 16px;color:#fff}.sub{color:#8fa8c7;margin-top:4px;font-size:13px}
.live{background:#073d35;color:#20ffc8;border:1px solid #19d9b5;border-radius:7px;padding:6px 12px;font-weight:800;font-size:12px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 #20ffc800}50%{box-shadow:0 0 18px #20ffc855}}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:15px}
.card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:20px;box-shadow:0 0 20px #0006}
.label{color:#7fa0c8;font-size:11px;letter-spacing:1px}.num{font-size:30px;font-weight:900;margin-top:10px}
.g{color:#20ff80}.r{color:#ff4f61}.c{color:#20e6c3}
.two{display:grid;grid-template-columns:2fr 1fr;gap:15px;margin-top:15px}
.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:15px;margin-top:15px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #17283d;font-size:13px}
th{color:#8fa8c7;font-size:11px;letter-spacing:1px}tr:last-child td{border-bottom:none}
.spark{height:50px;margin-top:8px;background:linear-gradient(135deg,transparent 45%,#18ff8044 46%,#18ff8033 55%,transparent 56%);border-radius:8px}
.footer{margin-top:15px;color:#627a99;font-size:12px}
[data-tab]{display:none}[data-tab].show{display:block}
@media(max-width:860px){.wrap{grid-template-columns:1fr}.side{display:none}.grid4,.two,.three{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
<aside class="side">
  <div class="logo"><div class="mark">A</div><div class="brand">ALPHA RADAR<span>SIGNALS</span></div></div>
  <div class="nav">
    <div id="nav-dashboard" class="act" onclick="showTab('dashboard')">Dashboard</div>
    <div id="nav-signals" onclick="showTab('signals')">Signals</div>
    <div id="nav-performance" onclick="showTab('performance')">Performance</div>
    <div id="nav-leaderboard" onclick="showTab('leaderboard')">Leaderboard</div>
    <div id="nav-settings" onclick="showTab('settings')">Settings</div>
    <div id="nav-health" onclick="showTab('health')">Health</div>
  </div>
  <div class="status"><b>Bot Status</b><p class="ok">● All Systems Operational</p><p style="color:#8fa8c7;margin-top:6px">Database OK<br>Redis OK<br>Telegram OK<br>Scanner OK</p></div>
</aside>
<main class="main">
  <div class="top">
    <div><h1>ALPHA RADAR SIGNALS</h1><div class="sub">Admin Dashboard</div></div>
    <div style="display:flex;align-items:center;gap:12px">
      <div id="last-update" style="color:#7fa0c8;font-size:12px">Updating...</div>
      <a href="/" style="color:#8fa8c7;font-size:13px;text-decoration:none">Public Site</a>
      <a href="/logout" style="color:#8fa8c7;font-size:13px;text-decoration:none">Logout</a>
      <span class="live">LIVE</span>
    </div>
  </div>

  <div id="tab-dashboard" data-tab class="show">
    <div class="grid4">
      <div class="card"><div class="label">WIN RATE (7D)</div><div id="winrate" class="num g">—</div></div>
      <div class="card"><div class="label">SIGNALS (7D)</div><div id="signals" class="num">—</div></div>
      <div class="card"><div class="label">AVG PNL</div><div id="avgpnl" class="num g">—</div></div>
      <div class="card"><div class="label">UNIVERSE</div><div id="universe" class="num c">—</div></div>
    </div>
    <div class="two">
      <div class="card"><h2>RECENT SIGNALS</h2>
        <table><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
        <tbody id="recent"></tbody></table>
      </div>
      <div class="card"><h2>SYSTEM</h2>
        <p>Scanner: <span class="g">Running</span></p>
        <p>Tracker: <span class="g">Running</span></p>
        <p>Dashboard: <span class="g">Online</span></p>
        <p style="margin-top:8px;color:#8fa8c7;font-size:12px">Port: 8010</p>
      </div>
    </div>
    <div class="three">
      <div class="card"><h2>MARKET REGIME</h2>
        <p>Bias: <span id="market-bias" class="c">--</span></p>
        <p>BTC 5m: <span id="btc-bias" class="c">--</span></p>
        <p>ETH 5m: <span id="eth-bias" class="c">--</span></p>
        <p>SOL 5m: <span id="sol-bias" class="c">--</span></p>
        <hr style="margin:14px 0;border-color:#1b2a41">
        <p>BTCUSDT: <span id="px-btc" class="c">--</span></p>
        <p>ETHUSDT: <span id="px-eth" class="c">--</span></p>
        <p>SOLUSDT: <span id="px-sol" class="c">--</span></p>
      </div>
      <div class="card"><h2>LEADERBOARD</h2><div id="dash-lb">Loading...</div></div>
      <div class="card"><h2>PERFORMANCE (7D)</h2><div class="spark"></div><div id="perf-sum" style="margin-top:10px;color:#8fa8c7;font-size:13px"></div></div>
    </div>
    <div class="footer">© 2026 ALPHA RADAR SIGNALS</div>
  </div>

  <div id="tab-signals" data-tab>
    <div class="card"><h2>LIVE SIGNALS</h2>
      <table><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
      <tbody id="signals-table"></tbody></table>
    </div>
  </div>

  <div id="tab-performance" data-tab>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0">PERFORMANCE</h2>
        <button id="rebuild-btn" onclick="doRebuild()"
          style="background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;font-weight:700;
                 font-size:12px;padding:8px 18px;border:0;border-radius:7px;cursor:pointer">
          Rebuild Performance
        </button>
      </div>
      <div id="rebuild-msg" style="font-size:12px;color:#8fa8c7;margin-bottom:14px;min-height:16px"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div style="background:#0b1320;border:1px solid #17314b;border-radius:10px;padding:14px">
          <div style="font-size:10px;color:#7fa0c8;letter-spacing:1px;text-transform:uppercase">Win Rate</div>
          <div id="perf-winrate" style="font-size:28px;font-weight:900;color:#20ff80;margin-top:6px">--</div>
        </div>
        <div style="background:#0b1320;border:1px solid #17314b;border-radius:10px;padding:14px">
          <div style="font-size:10px;color:#7fa0c8;letter-spacing:1px;text-transform:uppercase">Avg PnL / Trade</div>
          <div id="perf-pnl" style="font-size:28px;font-weight:900;color:#20ff80;margin-top:6px">--</div>
        </div>
        <div style="background:#0b1320;border:1px solid #17314b;border-radius:10px;padding:14px">
          <div style="font-size:10px;color:#7fa0c8;letter-spacing:1px;text-transform:uppercase">Profit Factor</div>
          <div id="perf-pf" style="font-size:28px;font-weight:900;color:#20e6c3;margin-top:6px">--</div>
        </div>
        <div style="background:#0b1320;border:1px solid #17314b;border-radius:10px;padding:14px">
          <div style="font-size:10px;color:#7fa0c8;letter-spacing:1px;text-transform:uppercase">Avg Risk/Reward</div>
          <div id="perf-rr" style="font-size:28px;font-weight:900;color:#ffd84d;margin-top:6px">--</div>
        </div>
      </div>
      <div style="margin-top:14px;padding:12px;background:#0b1320;border:1px solid #17314b;border-radius:10px;font-size:13px;color:#8fa8c7">
        Signals: <span id="perf-signals" style="color:#eaf2ff;font-weight:700">--</span>
        &nbsp;·&nbsp; Wins: <span id="perf-wins" style="color:#20ff80;font-weight:700">--</span>
        &nbsp;·&nbsp; Losses: <span id="perf-losses" style="color:#ff4f61;font-weight:700">--</span>
        &nbsp;·&nbsp; Open: <span id="perf-open" style="color:#20e6c3;font-weight:700">--</span>
      </div>
      <div id="perf-rebuilt-at" style="margin-top:8px;font-size:11px;color:#627a99"></div>
    </div>
  </div>

  <div id="tab-leaderboard" data-tab>
    <div class="card"><h2>TOP SYMBOLS</h2>
      <table><thead><tr><th>SYMBOL</th><th>AVG PNL</th><th>SIGNALS</th></tr></thead>
      <tbody id="lb-table"></tbody></table>
    </div>
  </div>

  <div id="tab-settings" data-tab>
    <div class="card"><h2>SETTINGS</h2>
      <p>VIP/Public routing enabled</p>
      <p>AI scoring active</p>
      <p>Daily reports active</p>
      <p>Production filters enabled</p>
      <p style="margin-top:12px;color:#8fa8c7;font-size:13px">Min confidence: <span id="cfg-conf">--</span></p>
      <p style="color:#8fa8c7;font-size:13px">Min RR: <span id="cfg-rr">--</span></p>
      <p style="color:#8fa8c7;font-size:13px">Max signals/hr: <span id="cfg-max">--</span></p>
      <p style="color:#8fa8c7;font-size:13px">Paper trading: <span id="cfg-paper">--</span></p>
    </div>
  </div>

  <div id="tab-health" data-tab>
    <div class="card"><h2>SYSTEM HEALTH</h2>
      <div id="health-status" style="font-size:13px;color:#8fa8c7">Loading...</div>
    </div>
    <div class="card" style="margin-top:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h2 style="margin:0">ACTIVE SIGNALS <span id="active-sig-count" style="font-size:14px;color:#20e6c3">—</span></h2>
        <span style="font-size:11px;color:#627a99">Duplicate guard: one active signal per symbol</span>
      </div>
      <table><thead><tr><th>SYMBOL</th><th>SIDE</th><th>STATUS</th><th>CONF</th><th>OPENED</th></tr></thead>
      <tbody id="active-sig-tbl"><tr><td colspan="5" style="text-align:center;color:#627a99;padding:12px">Loading...</td></tr></tbody></table>
    </div>
    <div class="card" style="margin-top:14px"><h2>AFFILIATE STATS</h2>
      <table><thead><tr><th>EXCHANGE</th><th>CLICKS</th></tr></thead>
      <tbody id="aff-tbl"></tbody></table>
    </div>
  </div>
</main>
</div>

<script>
async function load(){
  const r=await fetch('/api/dashboard');
  if(!r.ok)return;
  const d=await r.json();
  if(d.error)return;
  document.getElementById('winrate').textContent=d.winrate+'%';
  document.getElementById('signals').textContent=d.signals7d;
  document.getElementById('avgpnl').textContent=(d.avgpnl>=0?'+':'')+d.avgpnl+'%';
  document.getElementById('universe').textContent=d.universe;
  document.getElementById('perf-winrate').textContent=d.winrate+'%';
  document.getElementById('perf-pnl').textContent=(d.avgpnl>=0?'+':'')+d.avgpnl+'%';
  document.getElementById('perf-signals').textContent=d.signals7d;
  document.getElementById('perf-sum').innerHTML='Win rate: '+d.winrate+'%<br>Avg PnL: '+(d.avgpnl>=0?'+':'')+d.avgpnl+'%';
  // wins/losses from 7d window
  document.getElementById('perf-wins').textContent=d.wins??'--';
  document.getElementById('perf-losses').textContent=d.losses??'--';
  document.getElementById('perf-open').textContent=d.open_signals??'--';
  const lbH=(d.leaderboard||[]).map(x=>
    '<p><code>'+x.symbol+'</code> <span class="'+(x.avg>=0?'g':'r')+'">'+(x.avg>=0?'+':'')+x.avg+'%</span> ('+x.count+')</p>'
  ).join('');
  document.getElementById('dash-lb').innerHTML=lbH||'<p style="color:#8fa8c7">No data yet</p>';
  document.getElementById('lb-table').innerHTML=(d.leaderboard||[]).map(x=>
    '<tr><td>'+x.symbol+'</td><td class="'+(x.avg>=0?'g':'r')+'">'+(x.avg>=0?'+':'')+x.avg+'%</td><td>'+x.count+'</td></tr>'
  ).join('');
  const rows=(d.recent||[]).map(x=>
    '<tr><td>'+x.time+'</td><td>'+x.symbol+'</td>'+
    '<td class="'+(x.side==='LONG'?'g':'r')+'">'+x.side+'</td>'+
    '<td>'+x.tf+'</td><td>'+x.conf+'%</td><td>1:'+x.rr+'</td>'+
    '<td>'+x.status+'</td><td class="'+(x.pnl>=0?'g':'r')+'">'+x.pnl+'%</td></tr>'
  ).join('');
  document.getElementById('recent').innerHTML=rows;
  document.getElementById('signals-table').innerHTML=rows;
}
async function loadPx(){
  try{
    const r=await fetch('/api/prices');
    if(!r.ok)return;
    const d=await r.json();
    if(d.prices){
      document.getElementById('px-btc').textContent=d.prices.BTCUSDT??'--';
      document.getElementById('px-eth').textContent=d.prices.ETHUSDT??'--';
      document.getElementById('px-sol').textContent=d.prices.SOLUSDT??'--';
      if(d.market_bias){
        const mb=d.market_bias;
        const bEl=document.getElementById('market-bias');
        bEl.textContent=mb.bias;
        bEl.className=mb.bias==='RISK_ON'?'g':mb.bias==='RISK_OFF'?'r':'c';
        document.getElementById('btc-bias').textContent=mb.btc_5m_change_pct+'%';
        document.getElementById('eth-bias').textContent=mb.eth_5m_change_pct+'%';
        document.getElementById('sol-bias').textContent=mb.sol_5m_change_pct+'%';
      }
    }
  }catch(e){}
}
async function tick(){
  try{
    await load();
    document.getElementById('last-update').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById('last-update').textContent='Connection issue...';
  }
}
tick();loadPx();
setInterval(tick,5000);setInterval(loadPx,2000);

async function loadHealth(){
  try{
    const r=await fetch('/api/health');
    if(!r.ok)return;
    const d=await r.json();
    const c=d.components||{};
    const ok=v=>v?'<span class="g">● OK</span>':'<span class="r">● FAIL</span>';
    const ms=v=>v>=0?` (${v}ms)`:'';
    document.getElementById('health-status').innerHTML=
      `<p>Overall: ${d.ok?'<span class="g">✅ All Systems OK</span>':'<span class="r">❌ Degraded</span>'}</p>`+
      `<p style="margin-top:10px">Dashboard: ${ok(true)}</p>`+
      `<p>Database: ${ok(c.database?.ok)}${ms(c.database?.latency_ms)}</p>`+
      `<p>Redis: ${ok(c.redis?.ok)}${ms(c.redis?.latency_ms)}</p>`+
      `<p>WebSocket: ${ok(c.websocket?.ok)}</p>`+
      `<p style="margin-top:10px;color:#8fa8c7">Uptime: ${Math.floor(d.uptime_sec/3600)}h ${Math.floor((d.uptime_sec%3600)/60)}m</p>`+
      `<p style="color:#8fa8c7">Auto-trading: ${d.config?.auto_trading_enabled?'<span class="y">ENABLED</span>':'Disabled'}</p>`+
      `<p style="color:#8fa8c7">Paper trading: ${d.config?.paper_trading?'<span class="y">ENABLED</span>':'Disabled'}</p>`;
    if(d.config){
      document.getElementById('cfg-conf').textContent=d.config.min_confidence+'%';
      document.getElementById('cfg-rr').textContent='1:'+d.config.min_rr;
      document.getElementById('cfg-max').textContent=d.config.max_signals_per_hour;
      document.getElementById('cfg-paper').textContent=d.config.paper_trading?'ON':'OFF';
    }
  }catch(e){}
}

async function loadAffiliateStats(){
  try{
    const r=await fetch('/api/admin/affiliate-stats');
    if(!r.ok)return;
    const d=await r.json();
    document.getElementById('aff-tbl').innerHTML=(d||[]).map(x=>
      `<tr><td>${x.exchange}</td><td>${x.clicks}</td></tr>`
    ).join('')||'<tr><td colspan="2" style="text-align:center;color:#627a99;padding:14px">No clicks yet</td></tr>';
  }catch(e){}
}

async function loadActiveSignals(){
  try{
    const r=await fetch('/api/admin/active-signals');
    if(!r.ok)return;
    const d=await r.json();
    if(d.error)return;
    const rows=d.active||[];
    document.getElementById('active-sig-count').textContent='('+rows.length+')';
    document.getElementById('active-sig-tbl').innerHTML=rows.map(x=>
      '<tr>'+
      '<td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td class="bopen">'+x.status+'</td>'+
      '<td>'+x.confidence+'%</td>'+
      '<td style="color:#8fa8c7">'+x.opened+'</td>'+
      '</tr>'
    ).join('')||'<tr><td colspan="5" style="text-align:center;color:#20ff80;padding:12px">✅ No active signals — clean</td></tr>';
  }catch(e){}
}
setInterval(loadHealth,30000);
setInterval(loadAffiliateStats,60000);
setInterval(loadActiveSignals,15000);

function showTab(name){
  document.querySelectorAll('[data-tab]').forEach(el=>el.classList.remove('show'));
  document.querySelectorAll('.nav div').forEach(el=>el.classList.remove('act'));
  const t=document.getElementById('tab-'+name);
  if(t)t.classList.add('show');
  const n=document.getElementById('nav-'+name);
  if(n)n.classList.add('act');
  if(name==='health'){loadHealth();loadAffiliateStats();loadActiveSignals();}
  if(name==='performance'){loadPerfDetail();}
}
async function loadPerfDetail(){
  try{
    const r=await fetch('/api/public/performance');
    if(!r.ok)return;
    const d=await r.json();
    if(d.error)return;
    document.getElementById('perf-pf').textContent=d.profit_factor;
    document.getElementById('perf-rr').textContent='1:'+d.avg_rr;
  }catch(e){}
}
async function doRebuild(){
  const btn=document.getElementById('rebuild-btn');
  const msg=document.getElementById('rebuild-msg');
  btn.disabled=true;
  btn.textContent='Rebuilding…';
  msg.style.color='#8fa8c7';
  msg.textContent='Running performance rebuild — please wait…';
  try{
    const r=await fetch('/api/performance/rebuild');
    const d=await r.json();
    if(d.error){
      msg.textContent='❌ Error: '+d.error;
      msg.style.color='#ff4f61';
    }else{
      const sc=d.signal_count||{};
      msg.textContent='✅ Rebuilt '+new Date(d.rebuilt_at).toLocaleTimeString()+
        ' — '+sc.closed+' closed signals processed';
      msg.style.color='#20ff80';
      document.getElementById('perf-winrate').textContent=d.win_rate+'%';
      const pEl=document.getElementById('perf-pnl');
      pEl.textContent=(d.avg_pnl>=0?'+':'')+d.avg_pnl+'%';
      pEl.style.color=d.avg_pnl>=0?'#20ff80':'#ff4f61';
      document.getElementById('perf-pf').textContent=d.profit_factor;
      document.getElementById('perf-rr').textContent='1:'+d.avg_rr;
      document.getElementById('perf-signals').textContent=sc.closed??'--';
      document.getElementById('perf-wins').textContent=sc.wins??'--';
      document.getElementById('perf-losses').textContent=sc.losses??'--';
      document.getElementById('perf-open').textContent=sc.open??'--';
      document.getElementById('perf-rebuilt-at').textContent=
        'Last rebuilt: '+new Date(d.rebuilt_at).toLocaleString()+
        ' · daily_stats: '+d.daily_rows+' rows · weekly_stats: '+d.weekly_rows+' rows';
    }
  }catch(e){
    msg.textContent='❌ Request failed — check console';
    msg.style.color='#ff4f61';
  }
  btn.disabled=false;
  btn.textContent='Rebuild Performance';
}
</script>
</body>
</html>
"""
