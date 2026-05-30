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
from sqlalchemy import select, desc

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
    try:
        stats = await _get_stats()
        now = datetime.now(timezone.utc)
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
                .order_by(desc(Signal.created_at)).limit(1000)
            )
            closed = res.scalars().all()

        wins = [s for s in closed if s.status in ("TP1", "TP2", "TP3")]
        losses = [s for s in closed if s.status == "SL"]
        total_closed = len(closed)
        win_rate = len(wins) / max(1, total_closed) * 100
        avg_rr = sum(float(s.risk_reward or 0) for s in closed) / max(1, total_closed)

        # Profit factor: gross wins / gross losses (by pnl_pct)
        gross_win = sum(float(s.pnl_pct or 0) for s in wins)
        gross_loss = abs(sum(float(s.pnl_pct or 0) for s in losses))
        profit_factor = round(gross_win / max(0.01, gross_loss), 2)

        # LONG vs SHORT breakdown
        long_signals = [s for s in closed if s.side == "LONG"]
        short_signals = [s for s in closed if s.side == "SHORT"]
        long_wins = [s for s in long_signals if s.status in ("TP1", "TP2", "TP3")]
        short_wins = [s for s in short_signals if s.status in ("TP1", "TP2", "TP3")]

        # Monthly breakdown (last 6 months)
        monthly: dict = {}
        for s in closed:
            if s.created_at:
                key = s.created_at.strftime("%Y-%m")
                monthly.setdefault(key, {"wins": 0, "losses": 0, "pnl": 0.0})
                if s.status in ("TP1", "TP2", "TP3"):
                    monthly[key]["wins"] += 1
                else:
                    monthly[key]["losses"] += 1
                monthly[key]["pnl"] = round(monthly[key]["pnl"] + float(s.pnl_pct or 0), 2)
        monthly_list = sorted(
            [{"month": k, **v} for k, v in monthly.items()],
            key=lambda x: x["month"]
        )[-6:]

        # Average hold time (minutes) for closed signals
        hold_times = []
        for s in closed:
            if s.created_at and s.closed_at:
                delta = (s.closed_at - s.created_at).total_seconds() / 60
                hold_times.append(delta)
        avg_hold_min = round(sum(hold_times) / max(1, len(hold_times)), 0) if hold_times else None

        # Best / worst symbols
        sym_pnl: dict = {}
        for s in closed:
            sym_pnl.setdefault(s.symbol, []).append(float(s.pnl_pct or 0))
        sym_avgs = [
            {"symbol": k, "avg": round(sum(v) / len(v), 2), "count": len(v)}
            for k, v in sym_pnl.items()
        ]
        best_symbols = sorted(sym_avgs, key=lambda x: x["avg"], reverse=True)[:5]
        worst_symbols = sorted(sym_avgs, key=lambda x: x["avg"])[:5]

        return JSONResponse({
            "total_closed": total_closed,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "avg_pnl": round(sum(float(s.pnl_pct or 0) for s in closed) / max(1, total_closed), 2),
            "avg_rr": round(avg_rr, 2),
            "profit_factor": profit_factor,
            "avg_hold_min": avg_hold_min,
            "long": {
                "total": len(long_signals),
                "wins": len(long_wins),
                "win_rate": round(len(long_wins) / max(1, len(long_signals)) * 100, 1),
            },
            "short": {
                "total": len(short_signals),
                "wins": len(short_wins),
                "win_rate": round(len(short_wins) / max(1, len(short_signals)) * 100, 1),
            },
            "monthly": monthly_list,
            "leaderboard": stats.get("leaderboard", []),
            "best_symbols": best_symbols,
            "worst_symbols": worst_symbols,
        })
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


@app.get("/api/public/backtest")
async def api_public_backtest():
    """Backtest metrics computed from all closed MTF signals in the database."""
    import math as _math

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

        if not signals:
            return JSONResponse({
                "total": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "profit_factor": 0,
                "sharpe_ratio": 0, "max_drawdown_pct": 0,
                "avg_rr": 0, "rr_distribution": [],
            })

        wins = [s for s in signals if s.status in ("TP1", "TP2", "TP3")]
        losses = [s for s in signals if s.status == "SL"]
        pnls = [float(s.pnl_pct or 0) for s in signals]
        rrs = [float(s.risk_reward or 0) for s in signals]

        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = round(gross_win / max(0.001, gross_loss), 2)

        mean_pnl = sum(pnls) / max(1, len(pnls))
        if len(pnls) > 1:
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
            sharpe = round(mean_pnl / max(0.001, _math.sqrt(variance)), 2)
        else:
            sharpe = 0.0

        cum = peak = max_dd = 0.0
        equity_curve = []
        for p in pnls:
            cum += p
            equity_curve.append(round(cum, 2))
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd

        # RR distribution: bucket into ranges
        rr_buckets: dict = {}
        for rr in rrs:
            bucket = f"{_math.floor(rr * 2) / 2:.1f}"
            rr_buckets[bucket] = rr_buckets.get(bucket, 0) + 1
        rr_dist = sorted(
            [{"rr": k, "count": v} for k, v in rr_buckets.items()],
            key=lambda x: float(x["rr"])
        )

        # Monthly breakdown
        monthly: dict = {}
        for s in signals:
            if s.created_at:
                mo = s.created_at.strftime("%Y-%m")
                monthly.setdefault(mo, {"wins": 0, "losses": 0, "pnl": 0.0})
                if s.status in ("TP1", "TP2", "TP3"):
                    monthly[mo]["wins"] += 1
                else:
                    monthly[mo]["losses"] += 1
                monthly[mo]["pnl"] = round(monthly[mo]["pnl"] + float(s.pnl_pct or 0), 2)
        monthly_list = sorted(
            [{"month": k, **v} for k, v in monthly.items()],
            key=lambda x: x["month"]
        )

        return JSONResponse({
            "total": len(signals),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(1, len(signals)) * 100, 1),
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": round(max_dd, 2),
            "avg_rr": round(sum(rrs) / max(1, len(rrs)), 2),
            "avg_pnl": round(mean_pnl, 2),
            "rr_distribution": rr_dist,
            "monthly": monthly_list,
            "equity_curve": equity_curve[-60:],
        })
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
    wsh = ws_health()
    uptime = round(time.time() - _boot_time)

    # Database check
    db_ok = False
    db_latency_ms = -1
    try:
        import time as _time
        t0 = _time.monotonic()
        async with SessionLocal() as s:
            await s.execute(select(Signal).limit(1))
        db_latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        db_ok = True
    except Exception as exc:
        db_err = str(exc)[:100]

    # Redis check
    redis_ok = False
    redis_latency_ms = -1
    try:
        from app.market_data.cache import get_redis
        import time as _time
        t0 = _time.monotonic()
        r = await get_redis()
        await r.ping()
        redis_latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        redis_ok = True
    except Exception:
        pass

    return {
        "ok": db_ok and redis_ok,
        "brand": "ALPHA RADAR SIGNALS",
        "uptime_sec": uptime,
        "components": {
            "dashboard": {"ok": True, "detail": f"port {settings.dashboard_port}"},
            "database": {
                "ok": db_ok,
                "latency_ms": db_latency_ms,
            },
            "redis": {
                "ok": redis_ok,
                "latency_ms": redis_latency_ms,
            },
            "websocket": wsh,
        },
        "config": {
            "min_confidence": settings.min_confidence,
            "min_rr": settings.min_rr,
            "scan_interval_sec": settings.scan_interval_sec,
            "max_signals_per_hour": settings.max_signals_per_hour,
            "paper_trading": settings.paper_trading,
            "auto_trading_enabled": settings.auto_trading_enabled,
        },
    }


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

    # header nav community buttons
    tg_btn = (
        f'<a href="{tg_url}" target="_blank" style="background:#0088cc22;border:1px solid #0088cc;'
        f'color:#5bb7e3;padding:7px 13px;border-radius:7px;font-size:13px;text-decoration:none">Telegram</a>'
        if tg_url else ""
    )
    dc_btn = (
        f'<a href="{dc_url}" target="_blank" style="background:#5865f222;border:1px solid #5865f2;'
        f'color:#8b95f7;padding:7px 13px;border-radius:7px;font-size:13px;text-decoration:none">Discord</a>'
        if dc_url else ""
    )
    html = html.replace("__TG_BTN__", tg_btn).replace("__DC_BTN__", dc_btn)

    # hero CTA buttons
    hero_btns = []
    if tg_url:
        hero_btns.append(
            f'<a href="{tg_url}" target="_blank" style="background:linear-gradient(90deg,#0077b6,#00bbf0);'
            f'color:#fff;padding:14px 28px;border-radius:12px;font-weight:700;font-size:15px;text-decoration:none">✈ Join Telegram</a>'
        )
    if dc_url:
        hero_btns.append(
            f'<a href="{dc_url}" target="_blank" style="background:#5865f2;color:#fff;padding:14px 28px;'
            f'border-radius:12px;font-weight:700;font-size:15px;text-decoration:none">💬 Join Discord</a>'
        )
    if not hero_btns:
        hero_btns.append('<span style="color:#8fa8c7;font-size:15px">Free signals — join our community below</span>')
    html = html.replace("__HERO_BTNS__", "".join(hero_btns))

    # community section
    comm_cards = []
    if tg_url:
        comm_cards.append(
            f'<div class="comm-card"><div class="comm-ico">✈</div>'
            f'<div><div class="comm-t">Telegram Channel</div>'
            f'<div class="comm-d">Get free AI signals instantly in Telegram. Real-time alerts.</div>'
            f'<a href="{tg_url}" target="_blank" class="btn btntg">Join Free</a></div></div>'
        )
    if dc_url:
        comm_cards.append(
            f'<div class="comm-card"><div class="comm-ico">💬</div>'
            f'<div><div class="comm-t">Discord Server</div>'
            f'<div class="comm-d">Chat with traders, analysis channels and signal alerts.</div>'
            f'<a href="{dc_url}" target="_blank" class="btn btndc">Join Server</a></div></div>'
        )
    community_section = (
        '<div class="container"><section>'
        '<div class="stitle"><b></b>Join the Community</div>'
        '<div class="comm-grid">' + "".join(comm_cards) + '</div>'
        '</section></div>'
    ) if comm_cards else ""
    html = html.replace("__COMMUNITY__", community_section)

    # donate section
    don_cards = []
    if trc20:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">USDT</div><div class="don-net">TRC20 (Tron Network)</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{trc20}\')">{trc20}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    if bep20:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">USDT</div><div class="don-net">BEP20 (BSC Network)</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{bep20}\')">{bep20}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    if btc_addr:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">BTC</div><div class="don-net">Bitcoin Network</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{btc_addr}\')">{btc_addr}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    if eth_addr:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">ETH</div><div class="don-net">Ethereum (ERC20)</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{eth_addr}\')">{eth_addr}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    donate_section = (
        '<div class="container"><section>'
        '<div class="stitle"><b></b>Support the Project</div>'
        '<p style="color:#8fa8c7;margin-bottom:18px;font-size:14px">All signals are 100% free. Donations help keep the servers running.</p>'
        '<div class="don-grid">' + "".join(don_cards) + '</div>'
        '</section></div>'
    ) if don_cards else ""
    html = html.replace("__DONATE__", donate_section)

    # affiliates section
    aff_cards = []
    exchanges = [
        ("Binance", binance_aff, "#f0b90b", "World's largest crypto exchange"),
        ("Bybit", bybit_aff, "#ffcc00", "Top derivatives & futures"),
        ("OKX", okx_aff, "#1a82ff", "Leading altcoin exchange"),
        ("Bitget", bitget_aff, "#00e6b3", "Copy trading platform"),
    ]
    for name, url, color, desc in exchanges:
        if url:
            aff_cards.append(
                f'<div class="aff-card"><div class="aff-name" style="color:{color}">{name}</div>'
                f'<div class="aff-desc">{desc}</div>'
                f'<a href="{url}" target="_blank" class="btn-aff">Register Free</a></div>'
            )
    aff_section = (
        '<div class="container"><section>'
        '<div class="stitle"><b></b>Recommended Exchanges</div>'
        '<p style="color:#8fa8c7;margin-bottom:18px;font-size:14px">Register through our partner links to support free signals.</p>'
        '<div class="aff-grid">' + "".join(aff_cards) + '</div>'
        '</section></div>'
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
    body = """
<div class="page-title">Performance Statistics</div>
<div class="sbar" id="perf-bar">
  <div class="scard"><div class="slabel">WIN RATE</div><div id="p-wr" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">PROFIT FACTOR</div><div id="p-pf" class="sval c">—</div></div>
  <div class="scard"><div class="slabel">AVG RR</div><div id="p-rr" class="sval y">—</div></div>
  <div class="scard"><div class="slabel">TOTAL CLOSED</div><div id="p-tot" class="sval">—</div></div>
</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
  <div class="card">
    <div style="font-size:14px;font-weight:700;margin-bottom:14px;color:#eaf2ff">LONG Performance</div>
    <p>Total: <span id="l-tot" class="c">—</span></p>
    <p style="margin-top:8px">Wins: <span id="l-wins" class="g">—</span></p>
    <p style="margin-top:8px">Win Rate: <span id="l-wr" class="g">—</span></p>
  </div>
  <div class="card">
    <div style="font-size:14px;font-weight:700;margin-bottom:14px;color:#eaf2ff">SHORT Performance</div>
    <p>Total: <span id="s-tot" class="c">—</span></p>
    <p style="margin-top:8px">Wins: <span id="s-wins" class="g">—</span></p>
    <p style="margin-top:8px">Win Rate: <span id="s-wr" class="g">—</span></p>
  </div>
</div>
<div class="card" style="margin-top:16px">
  <div style="font-size:14px;font-weight:700;margin-bottom:14px;color:#eaf2ff">Monthly Performance</div>
  <table>
  <thead><tr><th>MONTH</th><th>WINS</th><th>LOSSES</th><th>NET PNL</th></tr></thead>
  <tbody id="monthly-tbl"><tr><td colspan="4" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr></tbody>
  </table>
</div>
<div class="card">
  <div style="font-size:14px;font-weight:700;margin-bottom:14px;color:#eaf2ff">Symbol Leaderboard</div>
  <table>
  <thead><tr><th>SYMBOL</th><th>AVG PNL</th><th>SIGNALS</th></tr></thead>
  <tbody id="lb-tbl"><tr><td colspan="3" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr></tbody>
  </table>
</div>"""
    js = """
async function load(){
  const r=await fetch('/api/public/performance');
  if(!r.ok)return;
  const d=await r.json();
  document.getElementById('p-wr').textContent=d.win_rate+'%';
  document.getElementById('p-pf').textContent=d.profit_factor;
  document.getElementById('p-rr').textContent='1:'+d.avg_rr;
  document.getElementById('p-tot').textContent=d.total_closed;
  document.getElementById('l-tot').textContent=d.long.total;
  document.getElementById('l-wins').textContent=d.long.wins;
  document.getElementById('l-wr').textContent=d.long.win_rate+'%';
  document.getElementById('s-tot').textContent=d.short.total;
  document.getElementById('s-wins').textContent=d.short.wins;
  document.getElementById('s-wr').textContent=d.short.win_rate+'%';
  document.getElementById('monthly-tbl').innerHTML=(d.monthly||[]).reverse().map(m=>
    '<tr><td>'+m.month+'</td><td class="g">'+m.wins+'</td><td class="r">'+m.losses+'</td>'+
    '<td class="'+(m.pnl>=0?'g':'r')+'">'+(m.pnl>=0?'+':'')+m.pnl+'%</td></tr>'
  ).join('')||'<tr><td colspan="4" style="text-align:center;color:#627a99;padding:18px">No data yet</td></tr>';
  document.getElementById('lb-tbl').innerHTML=(d.leaderboard||[]).map((x,i)=>
    '<tr><td><b>#'+(i+1)+' '+x.symbol+'</b></td>'+
    '<td class="'+(x.avg>=0?'g':'r')+'">'+(x.avg>=0?'+':'')+x.avg+'%</td>'+
    '<td>'+x.count+'</td></tr>'
  ).join('')||'<tr><td colspan="3" style="text-align:center;color:#627a99;padding:18px">No data yet</td></tr>';
}
document.addEventListener('DOMContentLoaded',load);
"""
    return _page_shell("Performance", body, extra_js=js)


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
    body = """
<div class="page-title">System Health</div>
<div id="hc-bar" class="sbar">
  <div class="scard"><div class="slabel">OVERALL</div><div id="hc-overall" class="sval c">—</div></div>
  <div class="scard"><div class="slabel">UPTIME</div><div id="hc-uptime" class="sval y">—</div></div>
  <div class="scard"><div class="slabel">LAST SIGNAL</div><div id="hc-lastsig" class="sval">—</div></div>
  <div class="scard"><div class="slabel">UNIVERSE</div><div id="hc-uni" class="sval c">—</div></div>
</div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:4px">
  <div class="card" id="hc-dashboard"><div class="slabel">DASHBOARD</div><div class="hc-status">—</div></div>
  <div class="card" id="hc-database"><div class="slabel">DATABASE</div><div class="hc-status">—</div></div>
  <div class="card" id="hc-redis"><div class="slabel">REDIS</div><div class="hc-status">—</div></div>
  <div class="card" id="hc-binance"><div class="slabel">BINANCE</div><div class="hc-status">—</div></div>
  <div class="card" id="hc-telegram"><div class="slabel">TELEGRAM</div><div class="hc-status">—</div></div>
  <div class="card" id="hc-scanner"><div class="slabel">SCANNER</div><div class="hc-status">—</div></div>
</div>
<div class="card" style="margin-top:16px">
  <div style="font-size:14px;font-weight:700;margin-bottom:12px;color:#eaf2ff">Configuration</div>
  <table id="hc-cfg-tbl" style="font-size:13px"><tbody>
    <tr><td colspan="2" style="color:#627a99;padding:14px;text-align:center">Loading...</td></tr>
  </tbody></table>
</div>"""
    css = """
.hc-status{font-size:20px;font-weight:900;margin-top:8px}
.online{color:#20ff80}.offline{color:#ff4f61}.degraded{color:#ffd84d}
"""
    js = """
function fmtUp(sec){
  const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;
  return h?h+'h '+m+'m':m+'m '+s+'s';
}
async function load(){
  try{
    const r=await fetch('/api/health');
    if(!r.ok)return;
    const d=await r.json();
    const c=d.components||{};
    const ok=d.ok;
    document.getElementById('hc-overall').textContent=ok?'ONLINE':'DEGRADED';
    document.getElementById('hc-overall').className='sval '+(ok?'g':'r');
    document.getElementById('hc-uptime').textContent=fmtUp(d.uptime_sec||0);
    document.getElementById('hc-uni').textContent=d.components?.dashboard?.detail||'—';

    function setComp(id,isOk,detail){
      const el=document.getElementById(id);
      const s=el.querySelector('.hc-status');
      s.textContent=isOk?'ONLINE':'OFFLINE';
      s.className='hc-status '+(isOk?'online':'offline');
      if(detail){
        let sub=el.querySelector('.hc-detail');
        if(!sub){sub=document.createElement('div');sub.className='hc-detail';sub.style.cssText='font-size:10px;color:#8fa8c7;margin-top:4px';el.appendChild(sub);}
        sub.textContent=detail;
      }
    }
    setComp('hc-dashboard',true,'port '+d.config?.scan_interval_sec||8010);
    setComp('hc-database',c.database?.ok,c.database?.latency_ms>=0?c.database.latency_ms+'ms latency':null);
    setComp('hc-redis',c.redis?.ok,c.redis?.latency_ms>=0?c.redis.latency_ms+'ms latency':null);
    setComp('hc-binance',c.websocket?.ok,'price feed');
    const tg=typeof d.config?.scan_interval_sec!=='undefined';
    setComp('hc-telegram',tg,'configured');
    setComp('hc-scanner',true,'running every '+d.config?.scan_interval_sec+'s');

    if(d.config){
      document.getElementById('hc-cfg-tbl').innerHTML=`
        <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0;width:50%">Min Confidence</td><td>${d.config.min_confidence}%</td></tr>
        <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Min RR</td><td>1:${d.config.min_rr}</td></tr>
        <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Scan Interval</td><td>${d.config.scan_interval_sec}s</td></tr>
        <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Max Signals/hr</td><td>${d.config.max_signals_per_hour}</td></tr>
        <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Paper Trading</td><td>${d.config.paper_trading?'<span class="y">ON</span>':'OFF'}</td></tr>
        <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Auto Trading</td><td>${d.config.auto_trading_enabled?'<span class="y">ON</span>':'OFF'}</td></tr>
      `;
    }
  }catch(e){console.error(e);}
}
async function loadLastSig(){
  try{
    const r=await fetch('/api/public/signals?limit=1');
    if(!r.ok)return;
    const d=await r.json();
    if(d&&d[0]&&d[0].created_at){
      const t=new Date(d[0].created_at);
      document.getElementById('hc-lastsig').textContent=t.toLocaleTimeString();
    }else{
      document.getElementById('hc-lastsig').textContent='None';
    }
  }catch(e){}
}
document.addEventListener('DOMContentLoaded',()=>{load();loadLastSig();setInterval(load,15000);setInterval(loadLastSig,30000);});
"""
    return _page_shell("Health Center", body, extra_css=css, extra_js=js)


def _paper_page_html() -> str:
    body = """
<div class="page-title">Paper Trading</div>
<div class="sbar">
  <div class="scard"><div class="slabel">BALANCE</div><div id="pt-bal" class="sval c">—</div></div>
  <div class="scard"><div class="slabel">TOTAL PNL</div><div id="pt-pnl" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">WIN RATE</div><div id="pt-wr" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">OPEN POSITIONS</div><div id="pt-open" class="sval y">—</div></div>
</div>
<div style="background:#0b2a0a;border:1px solid #1a5a18;border-radius:10px;padding:13px 16px;margin-bottom:16px;font-size:12px;color:#8fa8c7">
  <b style="color:#20ff80">Virtual Portfolio</b> — 10 000 USDT starting balance · 1% risk per trade · No real funds
</div>
<div class="card" style="margin-bottom:16px">
  <div style="font-size:14px;font-weight:700;margin-bottom:12px;color:#eaf2ff">Open Positions (<span id="pt-open-cnt">—</span>)</div>
  <div style="overflow-x:auto">
  <table>
  <thead><tr><th>OPENED</th><th>SYMBOL</th><th>SIDE</th><th>ENTRY</th><th>SL</th><th>TP1</th><th>CONF</th><th>RR</th></tr></thead>
  <tbody id="pt-open-tbl"><tr><td colspan="8" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr></tbody>
  </table>
  </div>
</div>
<div class="card">
  <div style="font-size:14px;font-weight:700;margin-bottom:12px;color:#eaf2ff">Closed Trades</div>
  <div style="overflow-x:auto">
  <table>
  <thead><tr><th>OPENED</th><th>SYMBOL</th><th>SIDE</th><th>ENTRY</th><th>STATUS</th><th>PNL%</th><th>PNL USDT</th></tr></thead>
  <tbody id="pt-closed-tbl"><tr><td colspan="7" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr></tbody>
  </table>
  </div>
</div>"""
    js = """
function pct(v){return(v>=0?'+':'')+v+'%';}
function cls(v){return v>=0?'g':'r';}
function sBadge(st){
  if(st==='OPEN')return '<span class="bopen">OPEN</span>';
  if(st==='SL')return '<span class="bsl">SL</span>';
  if(st==='EXPIRED')return '<span class="bexp">EXP</span>';
  return '<span class="btp">'+st+'</span>';
}
async function load(){
  const r=await fetch('/api/public/paper');
  if(!r.ok)return;
  const d=await r.json();
  if(d.error)return;
  document.getElementById('pt-bal').textContent='$'+d.current_balance.toFixed(2);
  const pEl=document.getElementById('pt-pnl');
  pEl.textContent=(d.total_pnl_usdt>=0?'+$':'−$')+Math.abs(d.total_pnl_usdt).toFixed(2);
  pEl.className='sval '+(d.total_pnl_usdt>=0?'g':'r');
  document.getElementById('pt-wr').textContent=d.win_rate+'%';
  document.getElementById('pt-open').textContent=d.open_count;
  document.getElementById('pt-open-cnt').textContent=d.open_count;
  document.getElementById('pt-open-tbl').innerHTML=(d.open||[]).map(x=>
    '<tr><td>'+x.opened+'</td><td><b><a href="/signal/'+x.id+'" style="color:#20e6c3">'+x.symbol+'</a></b></td>'+
    '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
    '<td>'+x.entry+'</td><td class="r">'+x.sl+'</td><td class="g">'+x.tp1+'</td>'+
    '<td>'+x.conf+'%</td><td>1:'+x.rr+'</td></tr>'
  ).join('')||'<tr><td colspan="8" style="text-align:center;color:#627a99;padding:14px">No open positions</td></tr>';
  document.getElementById('pt-closed-tbl').innerHTML=(d.closed||[]).map(x=>
    '<tr><td>'+x.opened+'</td><td><b><a href="/signal/'+x.id+'" style="color:#20e6c3">'+x.symbol+'</a></b></td>'+
    '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
    '<td>'+x.entry+'</td><td>'+sBadge(x.status)+'</td>'+
    '<td class="'+cls(x.pnl_pct)+'">'+pct(x.pnl_pct)+'</td>'+
    '<td class="'+cls(x.pnl_usdt)+'">'+(x.pnl_usdt>=0?'+$':'−$')+Math.abs(x.pnl_usdt).toFixed(2)+'</td></tr>'
  ).join('')||'<tr><td colspan="7" style="text-align:center;color:#627a99;padding:14px">No closed trades yet</td></tr>';
}
document.addEventListener('DOMContentLoaded',()=>{load();setInterval(load,15000);});
"""
    return _page_shell("Paper Trading", body, extra_js=js)


def _backtest_page_html() -> str:
    body = """
<div class="page-title">Backtest Engine</div>
<div class="sbar">
  <div class="scard"><div class="slabel">WIN RATE</div><div id="bt-wr" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">PROFIT FACTOR</div><div id="bt-pf" class="sval c">—</div></div>
  <div class="scard"><div class="slabel">MAX DRAWDOWN</div><div id="bt-dd" class="sval r">—</div></div>
  <div class="scard"><div class="slabel">SHARPE RATIO</div><div id="bt-sh" class="sval y">—</div></div>
</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:4px">
  <div class="card">
    <div style="font-size:14px;font-weight:700;margin-bottom:12px;color:#eaf2ff">Summary</div>
    <table style="font-size:13px"><tbody id="bt-summary">
      <tr><td colspan="2" style="color:#627a99;padding:14px;text-align:center">Loading...</td></tr>
    </tbody></table>
  </div>
  <div class="card">
    <div style="font-size:14px;font-weight:700;margin-bottom:12px;color:#eaf2ff">RR Distribution</div>
    <div id="bt-rr-dist" style="color:#627a99;padding:14px;text-align:center">Loading...</div>
  </div>
</div>
<div class="card" style="margin-top:16px">
  <div style="font-size:14px;font-weight:700;margin-bottom:12px;color:#eaf2ff">Monthly Breakdown</div>
  <div style="overflow-x:auto">
  <table>
  <thead><tr><th>MONTH</th><th>WINS</th><th>LOSSES</th><th>NET PNL</th></tr></thead>
  <tbody id="bt-monthly"><tr><td colspan="4" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr></tbody>
  </table>
  </div>
</div>
<div class="card" style="margin-top:16px">
  <div style="font-size:14px;font-weight:700;margin-bottom:12px;color:#eaf2ff">Cumulative PnL Curve</div>
  <div id="bt-curve" style="height:80px;display:flex;align-items:flex-end;gap:2px;overflow:hidden"></div>
</div>"""
    js = """
function pct(v){return(v>=0?'+':'')+v+'%';}
function cls(v){return v>=0?'g':'r';}
async function load(){
  const r=await fetch('/api/public/backtest');
  if(!r.ok)return;
  const d=await r.json();
  if(d.error)return;
  document.getElementById('bt-wr').textContent=d.win_rate+'%';
  document.getElementById('bt-pf').textContent=d.profit_factor;
  document.getElementById('bt-dd').textContent=pct(d.max_drawdown_pct);
  document.getElementById('bt-sh').textContent=d.sharpe_ratio;
  document.getElementById('bt-summary').innerHTML=`
    <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Total Signals</td><td>${d.total}</td></tr>
    <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Wins</td><td class="g">${d.wins}</td></tr>
    <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Losses</td><td class="r">${d.losses}</td></tr>
    <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Avg PnL/Trade</td><td class="${cls(d.avg_pnl)}">${pct(d.avg_pnl)}</td></tr>
    <tr><td style="color:#8fa8c7;padding:6px 14px 6px 0">Avg RR</td><td>1:${d.avg_rr}</td></tr>
  `;
  // RR dist bars
  const rrd=d.rr_distribution||[];
  const maxC=Math.max(1,...rrd.map(x=>x.count));
  document.getElementById('bt-rr-dist').innerHTML=rrd.map(x=>`
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px">
      <div style="width:40px;color:#8fa8c7;text-align:right">1:${x.rr}</div>
      <div style="flex:1;background:#0b1320;border-radius:3px;height:14px;overflow:hidden">
        <div style="background:linear-gradient(90deg,#08a98f,#20f0c0);width:${Math.round(x.count/maxC*100)}%;height:100%"></div>
      </div>
      <div style="width:24px;color:#eaf2ff">${x.count}</div>
    </div>`).join('')||'<p style="color:#627a99">No data</p>';
  // Monthly table
  document.getElementById('bt-monthly').innerHTML=(d.monthly||[]).reverse().map(m=>
    '<tr><td>'+m.month+'</td><td class="g">'+m.wins+'</td><td class="r">'+m.losses+'</td>'+
    '<td class="'+cls(m.pnl)+'">'+(m.pnl>=0?'+':'')+m.pnl+'%</td></tr>'
  ).join('')||'<tr><td colspan="4" style="text-align:center;color:#627a99;padding:14px">No data</td></tr>';
  // Equity curve
  const curve=d.equity_curve||[];
  if(curve.length>1){
    const mn=Math.min(...curve),mx=Math.max(...curve),range=mx-mn||1;
    const cols=curve.map(v=>{
      const h=Math.max(4,Math.round((v-mn)/range*76));
      return`<div style="flex:1;min-width:2px;height:${h}px;background:${v>=0?'#20ff80':'#ff4f61'};border-radius:1px 1px 0 0;opacity:0.8"></div>`;
    }).join('');
    document.getElementById('bt-curve').innerHTML=cols;
  }else{
    document.getElementById('bt-curve').innerHTML='<p style="color:#627a99;padding:14px">Not enough data</p>';
  }
}
document.addEventListener('DOMContentLoaded',load);
"""
    return _page_shell("Backtest Engine", body, extra_js=js)


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
<title>ALPHA RADAR SIGNALS — Free AI Crypto Futures Signals</title>
<meta name="description" content="Free AI-powered crypto futures signals. Multi-timeframe analysis. Real-time results."/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#070b12;color:#eaf2ff;font-family:Inter,Arial,sans-serif;line-height:1.6}
a{color:#20e6c3;text-decoration:none}
.container{max-width:1200px;margin:0 auto;padding:0 24px}
header{background:#08111c;border-bottom:1px solid #13263a;position:sticky;top:0;z-index:100}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:13px 24px;max-width:1200px;margin:0 auto;gap:12px}
.logo{display:flex;align-items:center;gap:11px;flex-shrink:0}
.mark{width:42px;height:42px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:20px;box-shadow:0 0 16px #00ffc855;flex-shrink:0}
.brand{font-size:16px;font-weight:900;letter-spacing:1px;color:#eaf2ff}
.brand em{color:#20f0c0;font-style:normal}
.hnav{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.live{background:#073d35;color:#20ffc8;border:1px solid #19d9b5;border-radius:5px;padding:3px 9px;font-weight:800;font-size:11px;animation:pulse 2s infinite;white-space:nowrap}
@keyframes pulse{0%,100%{box-shadow:0 0 0 #20ffc800}50%{box-shadow:0 0 14px #20ffc855}}
.hero-wrap{background:radial-gradient(ellipse 80% 40% at 50% 0%,#0d2a1e,transparent);padding:68px 24px 48px;text-align:center}
.hero-wrap h1{font-size:46px;font-weight:900;letter-spacing:2px;margin-bottom:14px}
.hero-wrap h1 em{color:#20f0c0;font-style:normal}
.hero-wrap p{font-size:16px;color:#8fa8c7;max-width:540px;margin:0 auto 34px}
.hero-btns{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.sbar{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:28px 0}
.scard{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:19px;text-align:center}
.slabel{color:#7fa0c8;font-size:10px;letter-spacing:2px;text-transform:uppercase}
.sval{font-size:32px;font-weight:900;margin-top:8px}
.g{color:#20ff80}.r{color:#ff4f61}.c{color:#20e6c3}.y{color:#ffd84d}
section{padding:36px 0}
.stitle{font-size:19px;font-weight:900;margin-bottom:18px;display:flex;align-items:center;gap:10px}
.stitle b{width:4px;height:21px;background:linear-gradient(180deg,#08a98f,#20f0c0);border-radius:2px;display:inline-block;flex-shrink:0}
.card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:20px;box-shadow:0 0 18px #0005}
.mkt-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}
.mkt-card{text-align:center;padding:16px;background:#0b1320;border:1px solid #17314b;border-radius:10px}
.mkt-sym{font-size:10px;color:#8fa8c7;letter-spacing:1px;margin-bottom:5px;text-transform:uppercase}
.mkt-px{font-size:24px;font-weight:900}
.bias-row{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;background:#0b1320;border:1px solid #17314b;border-radius:10px;padding:13px}
.bi{text-align:center}.bi .bl{font-size:9px;color:#8fa8c7;margin-bottom:2px;text-transform:uppercase;letter-spacing:1px}.bi .bv{font-size:14px;font-weight:700}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #17283d;font-size:13px}
th{color:#8fa8c7;font-size:10px;letter-spacing:1px;text-transform:uppercase}
tr:last-child td{border-bottom:none}
.bl2{background:#0a3a1f44;color:#20ff80;border:1px solid #20ff8033;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}
.bs2{background:#3a0a1244;color:#ff4f61;border:1px solid #ff4f6133;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}
.bopen{color:#20ffc8;font-weight:700}.btp{color:#20ff80;font-weight:700}.bsl{color:#ff4f61;font-weight:700}.bexp{color:#ffd84d;font-weight:700}
.stats3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.tabs{display:flex;gap:7px;margin-bottom:14px}
.tbtn{padding:7px 15px;border-radius:7px;border:1px solid #17314b;background:transparent;color:#8fa8c7;cursor:pointer;font-size:12px}
.tbtn.act{background:#08a98f22;border-color:#20f0c0;color:#20f0c0}
.tpane{display:none}.tpane.act{display:block}
.lb-row{display:flex;align-items:center;gap:11px;padding:10px 0;border-bottom:1px solid #17283d}
.lb-row:last-child{border-bottom:none}
.lb-rank{font-size:16px;font-weight:900;width:28px;color:#8fa8c7}
.lb-sym{font-size:14px;font-weight:700;flex:1}
.lb-avg{font-size:14px;font-weight:900}.lb-cnt{font-size:10px;color:#8fa8c7}
.comm-grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.comm-card{background:#0b1320;border:1px solid #17314b;border-radius:11px;padding:20px;display:flex;align-items:flex-start;gap:14px}
.comm-ico{font-size:32px;line-height:1;flex-shrink:0}
.comm-t{font-size:14px;font-weight:700;margin-bottom:3px}
.comm-d{font-size:12px;color:#8fa8c7;margin-bottom:11px}
.btn{padding:8px 16px;border-radius:7px;font-weight:700;font-size:12px;display:inline-block;cursor:pointer;text-decoration:none}
.btntg{background:#0088cc;color:#fff}.btndc{background:#5865f2;color:#fff}
.don-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:13px}
.don-card{background:#0b1320;border:1px solid #17314b;border-radius:11px;padding:17px}
.don-coin{font-size:11px;font-weight:700;color:#8fa8c7;letter-spacing:1px;margin-bottom:3px}
.don-net{font-size:10px;color:#627a99;margin-bottom:7px}
.don-addr{background:#070b12;border:1px solid #17314b;border-radius:7px;padding:8px;font-family:monospace;font-size:11px;color:#20e6c3;word-break:break-all;cursor:pointer;transition:border-color .2s}
.don-addr:hover{border-color:#20f0c0}
.don-hint{font-size:10px;color:#627a99;margin-top:4px}
.aff-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:13px}
.aff-card{background:#0b1320;border:1px solid #17314b;border-radius:11px;padding:17px;text-align:center}
.aff-name{font-size:15px;font-weight:900;margin-bottom:4px}
.aff-desc{font-size:11px;color:#8fa8c7;margin-bottom:11px}
.btn-aff{background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;font-weight:700;font-size:12px;padding:7px 12px;border-radius:7px;display:block;text-align:center;text-decoration:none}
.disc{background:#1a0c0c;border:1px solid #5a1a1a;border-radius:13px;padding:20px;margin-bottom:40px}
.disc h3{color:#ff7b8a;margin-bottom:9px;font-size:14px;display:flex;align-items:center;gap:6px}
.disc p{color:#c57a7a;font-size:13px;line-height:1.7}
.faq-list{display:flex;flex-direction:column;gap:10px}
.faq-item{background:#0b1320;border:1px solid #17314b;border-radius:10px;padding:16px 18px}
.faq-q{font-size:14px;font-weight:700;color:#eaf2ff;margin-bottom:6px}
.faq-a{font-size:13px;color:#8fa8c7;line-height:1.6}
footer{border-top:1px solid #13263a;padding:26px 24px;text-align:center;color:#627a99;font-size:12px}
@media(max-width:860px){.sbar{grid-template-columns:1fr 1fr}.mkt-grid{grid-template-columns:1fr}.comm-grid{grid-template-columns:1fr}.aff-grid{grid-template-columns:1fr 1fr}.stats3{grid-template-columns:1fr}.hero-wrap h1{font-size:30px}}
@media(max-width:480px){.sbar{grid-template-columns:1fr}.aff-grid{grid-template-columns:1fr}.don-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<header>
<div class="hdr">
<div class="logo">
  <div class="mark">A</div>
  <div class="brand">ALPHA RADAR <em>SIGNALS</em></div>
</div>
<div class="hnav">
  <span class="live">● LIVE</span>
  __TG_BTN__
  __DC_BTN__
  <a href="/admin" style="color:#627a99;font-size:11px;padding:5px 11px;border:1px solid #17314b;border-radius:6px">Admin</a>
</div>
</div>
</header>

<div class="hero-wrap">
<div class="container">
  <h1>ALPHA RADAR <em>SIGNALS</em></h1>
  <p>Free AI-powered crypto futures signals. Multi-timeframe analysis. Real-time results. No subscription.</p>
  <div class="hero-btns">__HERO_BTNS__</div>
</div>
</div>

<div class="container">
<div class="sbar">
  <div class="scard"><div class="slabel">WIN RATE (7D)</div><div id="s-wr" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">SIGNALS (7D)</div><div id="s-tot" class="sval">—</div></div>
  <div class="scard"><div class="slabel">AVG PNL</div><div id="s-pnl" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">UNIVERSE</div><div id="s-uni" class="sval c">—</div></div>
</div>

<section>
<div class="stitle"><b></b>Live Market Overview</div>
<div class="card">
  <div class="mkt-grid">
    <div class="mkt-card"><div class="mkt-sym">BTC/USDT</div><div id="px-btc" class="mkt-px c">—</div></div>
    <div class="mkt-card"><div class="mkt-sym">ETH/USDT</div><div id="px-eth" class="mkt-px c">—</div></div>
    <div class="mkt-card"><div class="mkt-sym">SOL/USDT</div><div id="px-sol" class="mkt-px c">—</div></div>
  </div>
  <div class="bias-row">
    <div class="bi"><div class="bl">MARKET BIAS</div><div id="b-ov" class="bv c">—</div></div>
    <div class="bi"><div class="bl">BTC 5M</div><div id="b-btc" class="bv">—</div></div>
    <div class="bi"><div class="bl">ETH 5M</div><div id="b-eth" class="bv">—</div></div>
    <div class="bi"><div class="bl">SOL 5M</div><div id="b-sol" class="bv">—</div></div>
    <div class="bi"><div class="bl">UPDATED</div><div id="b-upd" class="bv" style="color:#8fa8c7;font-size:11px">—</div></div>
  </div>
</div>
</section>

<section>
<div class="stitle"><b></b>Latest Signals</div>
<div class="card" style="overflow-x:auto">
<table>
<thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
<tbody id="sig-tbl"><tr><td colspan="8" style="text-align:center;color:#627a99;padding:24px">Loading signals...</td></tr></tbody>
</table>
</div>
</section>

<section>
<div class="stitle"><b></b>Performance Statistics (7D)</div>
<div class="stats3">
  <div class="card" style="text-align:center">
    <div class="slabel">WIN RATE</div>
    <div id="ps-wr" class="sval g" style="font-size:38px;margin:10px 0">—</div>
    <div style="font-size:12px;color:#627a99"><span id="ps-w" class="g">—</span> wins &nbsp;/&nbsp; <span id="ps-l" class="r">—</span> losses</div>
  </div>
  <div class="card" style="text-align:center">
    <div class="slabel">AVG PNL / TRADE</div>
    <div id="ps-pnl" class="sval g" style="font-size:38px;margin:10px 0">—</div>
    <div style="font-size:12px;color:#627a99">Closed trades only</div>
  </div>
  <div class="card" style="text-align:center">
    <div class="slabel">OPEN NOW</div>
    <div id="ps-open" class="sval c" style="font-size:38px;margin:10px 0">—</div>
    <div style="font-size:12px;color:#627a99">Active signals</div>
  </div>
</div>
</section>

<section>
<div class="stitle"><b></b>Signal History</div>
<div class="card">
<div class="tabs">
  <button class="tbtn act" onclick="swTab('open',this)">Open (<span id="tc-open">—</span>)</button>
  <button class="tbtn" onclick="swTab('closed',this)">Closed</button>
</div>
<div id="tp-open" class="tpane act" style="overflow-x:auto">
<table>
<thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>ENTRY</th><th>TP1</th><th>SL</th><th>CONF</th><th>RR</th></tr></thead>
<tbody id="open-tbl"><tr><td colspan="9" style="text-align:center;color:#627a99;padding:18px">No open signals</td></tr></tbody>
</table>
</div>
<div id="tp-closed" class="tpane" style="overflow-x:auto">
<table>
<thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>RESULT</th><th>PNL</th></tr></thead>
<tbody id="closed-tbl"><tr><td colspan="8" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr></tbody>
</table>
</div>
</div>
</section>

<section>
<div class="stitle"><b></b>Performance Leaderboard</div>
<div class="card"><div id="lb-list"><p style="color:#8fa8c7;text-align:center;padding:24px">Loading...</p></div></div>
</section>
</div>

__COMMUNITY__
__DONATE__
__AFFILIATES__
<div class="container">
<section>
<div class="stitle"><b></b>Frequently Asked Questions</div>
<div class="faq-list">
  <div class="faq-item"><div class="faq-q">Are the signals free?</div><div class="faq-a">Yes, 100% free. No subscription required. All signals are delivered directly to Telegram at no cost.</div></div>
  <div class="faq-item"><div class="faq-q">How do I receive signals?</div><div class="faq-a">Join our Telegram channel. Signals are posted automatically the moment the AI engine detects a valid setup.</div></div>
  <div class="faq-item"><div class="faq-q">What markets are covered?</div><div class="faq-a">We scan USDT perpetual futures on Binance — all liquid pairs with over $5M daily volume.</div></div>
  <div class="faq-item"><div class="faq-q">What does confidence % mean?</div><div class="faq-a">Confidence is the AI engine's 4-layer quality score (75–100%). Higher = more timeframe confluences aligned. It is not a win probability.</div></div>
  <div class="faq-item"><div class="faq-q">What is the 4-layer MTF pipeline?</div><div class="faq-a">Each signal passes four hard gates: 1D Trend → 4H Structure → 1H Setup → 15M Entry. All four must confirm before a signal is emitted.</div></div>
  <div class="faq-item"><div class="faq-q">What is Risk/Reward (RR)?</div><div class="faq-a">RR is the ratio of potential profit to potential loss. We require a minimum of 1:2.0 — you can gain at least $2 for every $1 risked.</div></div>
  <div class="faq-item"><div class="faq-q">Should I use all my capital on one signal?</div><div class="faq-a">Never. Risk at most 1–2% of your trading capital per trade. Proper position sizing is essential for long-term survival.</div></div>
  <div class="faq-item"><div class="faq-q">Who runs this project?</div><div class="faq-a">ALPHA RADAR SIGNALS is an independent trading tools project. We are not a registered financial institution. All signals are for educational use only.</div></div>
</div>
</section>
</div>

<div class="container">
<div class="disc">
<h3>⚠ Risk Disclaimer</h3>
<p>Signals are for educational purposes only. Trading futures is high risk. Past performance is not indicative of future results. You may lose all your capital. Never trade with money you cannot afford to lose. Alpha Radar Signals does not provide financial, investment, or legal advice. Always do your own research. By using this service you acknowledge and accept all trading risks.</p>
</div>
</div>

<footer>
<p style="font-size:15px;font-weight:700;color:#eaf2ff;margin-bottom:5px">ALPHA RADAR SIGNALS</p>
<p>Free AI-powered crypto futures signals &nbsp;·&nbsp; For educational use only</p>
<p style="margin-top:8px">
  <a href="/signals" style="color:#627a99;font-size:11px">Signals</a> &nbsp;·&nbsp;
  <a href="/performance" style="color:#627a99;font-size:11px">Performance</a> &nbsp;·&nbsp;
  <a href="/stats" style="color:#627a99;font-size:11px">Stats</a> &nbsp;·&nbsp;
  <a href="/about" style="color:#627a99;font-size:11px">About</a> &nbsp;·&nbsp;
  <a href="/faq" style="color:#627a99;font-size:11px">FAQ</a>
</p>
<p style="margin-top:6px">
  <a href="/terms" style="color:#627a99;font-size:11px">Terms</a> &nbsp;·&nbsp;
  <a href="/privacy" style="color:#627a99;font-size:11px">Privacy</a> &nbsp;·&nbsp;
  <a href="/risk-disclaimer" style="color:#627a99;font-size:11px">Risk Disclaimer</a> &nbsp;·&nbsp;
  <a href="/admin" style="color:#627a99;font-size:11px">Admin</a>
</p>
<p style="margin-top:8px;font-size:11px">© 2026 ALPHA RADAR SIGNALS &nbsp;·&nbsp; Not financial advice.</p>
</footer>

<script>
function pct(v){return(v===null||v===undefined)?'—':(v>=0?'+':'')+v+'%';}
function cls(v){return v>=0?'g':'r';}
function sBadge(st){
  if(st==='OPEN')return '<span class="bopen">OPEN</span>';
  if(st==='SL')return '<span class="bsl">SL</span>';
  if(st==='EXPIRED')return '<span class="bexp">EXP</span>';
  return '<span class="btp">'+st+'</span>';
}
function swTab(n,btn){
  document.querySelectorAll('.tbtn').forEach(b=>b.classList.remove('act'));
  btn.classList.add('act');
  document.querySelectorAll('.tpane').forEach(p=>p.classList.remove('act'));
  document.getElementById('tp-'+n).classList.add('act');
}
async function copyAddr(el,addr){
  try{
    await navigator.clipboard.writeText(addr);
    el.style.borderColor='#20f0c0';
    el.title='Copied!';
    setTimeout(()=>{el.style.borderColor='';el.title='';},1600);
  }catch(e){}
}
async function loadStats(){
  try{
    const r=await fetch('/api/public/stats');
    if(!r.ok)return;
    const d=await r.json();
    if(d.error)return;
    document.getElementById('s-wr').textContent=d.winrate+'%';
    document.getElementById('s-tot').textContent=d.signals7d;
    const pe=document.getElementById('s-pnl');
    pe.textContent=pct(d.avgpnl);pe.className='sval '+cls(d.avgpnl);
    document.getElementById('s-uni').textContent=d.universe;
    document.getElementById('ps-wr').textContent=d.winrate+'%';
    const ppe=document.getElementById('ps-pnl');
    ppe.textContent=pct(d.avgpnl);ppe.className='sval '+cls(d.avgpnl);
    document.getElementById('ps-w').textContent=d.wins??'—';
    document.getElementById('ps-l').textContent=d.losses??'—';
    document.getElementById('ps-open').textContent=d.open_signals??'—';
    document.getElementById('tc-open').textContent=d.open_signals??'—';
    // latest signals
    const sRows=(d.recent||[]).slice(0,15).map(x=>
      '<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.tf+'</td><td>'+x.conf+'%</td><td>1:'+x.rr+'</td>'+
      '<td>'+sBadge(x.status)+'</td>'+
      '<td class="'+cls(x.pnl)+'">'+pct(x.pnl)+'</td></tr>'
    ).join('');
    document.getElementById('sig-tbl').innerHTML=sRows||
      '<tr><td colspan="8" style="text-align:center;color:#627a99;padding:20px">No signals yet</td></tr>';
    // open history
    const oRows=(d.open||[]).map(x=>
      '<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.tf+'</td><td>'+x.entry_low+'</td><td>'+x.tp1+'</td><td>'+x.sl+'</td>'+
      '<td>'+x.conf+'%</td><td>1:'+x.rr+'</td></tr>'
    ).join('');
    document.getElementById('open-tbl').innerHTML=oRows||
      '<tr><td colspan="9" style="text-align:center;color:#627a99;padding:18px">No open signals</td></tr>';
    // closed history
    const cRows=(d.closed_recent||[]).map(x=>
      '<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.tf+'</td><td>'+x.conf+'%</td><td>1:'+x.rr+'</td>'+
      '<td>'+sBadge(x.status)+'</td>'+
      '<td class="'+cls(x.pnl)+'">'+pct(x.pnl)+'</td></tr>'
    ).join('');
    document.getElementById('closed-tbl').innerHTML=cRows||
      '<tr><td colspan="8" style="text-align:center;color:#627a99;padding:18px">No closed signals</td></tr>';
    // leaderboard
    const lbHtml=(d.leaderboard||[]).map((x,i)=>{
      const clr=i===0?'#ffd84d':i===1?'#c0c0c0':i===2?'#cd7f32':'#8fa8c7';
      return '<div class="lb-row">'+
        '<div class="lb-rank" style="color:'+clr+'">#'+(i+1)+'</div>'+
        '<div class="lb-sym">'+x.symbol+'</div>'+
        '<div style="text-align:right">'+
          '<div class="lb-avg '+cls(x.avg)+'">'+pct(x.avg)+'</div>'+
          '<div class="lb-cnt">'+x.count+' signals</div>'+
        '</div></div>';
    }).join('');
    document.getElementById('lb-list').innerHTML=lbHtml||
      '<p style="color:#8fa8c7;text-align:center;padding:20px">No data yet</p>';
  }catch(e){console.error(e);}
}
async function loadPrices(){
  try{
    const r=await fetch('/api/public/prices');
    if(!r.ok)return;
    const d=await r.json();
    if(d.prices){
      document.getElementById('px-btc').textContent=d.prices.BTCUSDT??'—';
      document.getElementById('px-eth').textContent=d.prices.ETHUSDT??'—';
      document.getElementById('px-sol').textContent=d.prices.SOLUSDT??'—';
    }
    if(d.market_bias){
      const mb=d.market_bias;
      const bEl=document.getElementById('b-ov');
      bEl.textContent=mb.bias??'—';
      bEl.className='bv '+(mb.bias==='RISK_ON'?'g':mb.bias==='RISK_OFF'?'r':'c');
      document.getElementById('b-btc').textContent=(mb.btc_5m_change_pct??'—')+'%';
      document.getElementById('b-eth').textContent=(mb.eth_5m_change_pct??'—')+'%';
      document.getElementById('b-sol').textContent=(mb.sol_5m_change_pct??'—')+'%';
    }
    document.getElementById('b-upd').textContent=new Date().toLocaleTimeString();
  }catch(e){console.error(e);}
}
loadStats();loadPrices();
setInterval(loadStats,6000);
setInterval(loadPrices,3000);
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
