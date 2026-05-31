from __future__ import annotations

import html as html_lib
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from sqlalchemy import func as _sqlfunc, select, desc

from app.config import settings
from app.database.session import SessionLocal
from app.database.models import FundingRateSnapshot, Signal, AffiliateClick, OpenInterestSnapshot, PaperPosition
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

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

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


@app.get("/api/public/diagnostics/{signal_id}")
async def api_public_diagnostics(signal_id: int):
    """
    Sprint 16A — Return full diagnostics for a signal.
    Includes per-layer scores, funding/OI/liquidity scores, and RR method.
    """
    import json as _json
    try:
        async with SessionLocal() as session:
            sig = await session.get(Signal, signal_id)
        if sig is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        diag: dict = {}
        if sig.diagnostics:
            try:
                diag = _json.loads(sig.diagnostics)
            except Exception:
                diag = {}
        return JSONResponse({
            "signal_id":  signal_id,
            "symbol":     sig.symbol,
            "side":       sig.side,
            "confidence": round(float(sig.confidence or 0), 1),
            "rr_method":  sig.rr_method or "atr",
            "diagnostics": diag,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/public/winrate-analysis")
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


@app.get("/api/public/languages")
async def api_public_languages():
    """List of all supported UI languages with code and native name."""
    from app.dashboard.i18n import SUPPORTED_LANGUAGES
    return JSONResponse({"languages": SUPPORTED_LANGUAGES, "count": len(SUPPORTED_LANGUAGES)})


@app.get("/api/public/translations")
async def api_public_translations(lang: str = "en"):
    """Lazy-load the full translation map for *lang*. Falls back to English."""
    import re as _re
    safe = _re.sub(r"[^a-zA-Z]", "", lang)[:8].lower() or "en"
    from app.dashboard.i18n import load_locale
    return JSONResponse(load_locale(safe))


@app.get("/api/public/strategy")
async def api_public_strategy():
    """Public strategy engine config — exact filters and logic descriptions powering the bot."""
    entry_pass = int(os.getenv("ENTRY_PASS_SCORE", str(settings.entry_pass_score)))
    cooldown_min = round(settings.signal_cooldown_sec / 60, 1)
    return JSONResponse({
        "timeframes": {
            "trend":     "1D",
            "structure": "4H",
            "setup":     "1H",
            "entry":     "15M",
        },
        "filters": {
            "min_confidence":      settings.min_confidence,
            "min_rr":              settings.min_rr,
            "entry_pass_score":    entry_pass,
            "max_signals_per_hour": settings.max_signals_per_hour,
            "cooldown_seconds":    settings.signal_cooldown_sec,
        },
        "strategy": {
            "trend_engine": (
                "EMA50 vs EMA200 cross required. LONG: EMA50 > EMA200. "
                "SHORT: EMA50 < EMA200. BOS/MSS market structure confirmation. "
                "Score: 10 base + EMA separation bonus (max 5) + structure bonus (5) = max 20 pts."
            ),
            "structure_engine": (
                "4H confluence: BOS · CHoCH · Order Block · Fair Value Gap · Liquidity Sweep. "
                "Minimum 2/5 hits required. Each extra hit adds confidence bonus."
            ),
            "setup_engine": (
                "1H conditions: Pullback zone · Retest · VWAP alignment · EMA alignment · Volume spike. "
                "Minimum 3/5 conditions required. Extra hits add confidence bonus."
            ),
            "entry_engine": (
                f"15M factors (each 1 pt, max 5): BOS · FVG retest · OB retest · "
                f"EMA pullback · VWAP reclaim. Minimum {entry_pass}/5 required to trigger entry."
            ),
            "funding_engine": (
                "Funding rate classification: Neutral (|rate| < 0.03%), "
                "Crowded Long (rate > 0.08%), Crowded Short (rate < -0.08%). "
                "Extreme funding blocks signal emission. Filter weight: 10 pts."
            ),
            "risk_engine": (
                f"Confidence gate: ≥ {settings.min_confidence}%. "
                f"RR gate: ≥ 1:{settings.min_rr}. "
                f"Rate limiter: max {settings.max_signals_per_hour}/hr. "
                f"Cooldown: {cooldown_min} min per symbol+side."
            ),
        },
    })


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


# ── Lightweight in-memory cache (Sprint 12–13) ────────────────────
_api_cache: dict = {}
_API_CACHE_TTL = 45.0  # seconds


def _cache_get(key: str):
    entry = _api_cache.get(key)
    if entry and time.time() - entry[0] < _API_CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, val):
    _api_cache[key] = (time.time(), val)


# ── Sprint 12: Performance Analytics Center ───────────────────────

@app.get("/api/public/performance-center")
async def api_performance_center():
    """Multi-period signal analytics: 24h/7D/30D, bands, pairs, distribution."""
    cached = _cache_get("perf_center")
    if cached is not None:
        return JSONResponse(cached)

    from collections import defaultdict as _dd

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d  = now - timedelta(days=7)
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
            open_s  = [s for s in sigs if s.status == "OPEN"]
            tp1 = [s for s in sigs if s.status == "TP1"]
            tp2 = [s for s in sigs if s.status == "TP2"]
            sl  = [s for s in sigs if s.status == "SL"]
            exp = [s for s in sigs if s.status == "EXPIRED"]
            wins = [s for s in closed if s.status in WIN_ST]
            nc = max(1, len(closed))
            wr = round(len(wins) / nc * 100, 1) if len(closed) >= 5 else None
            return {
                "total": len(sigs), "closed": len(closed),
                "tp1": len(tp1), "tp2": len(tp2),
                "sl": len(sl), "expired": len(exp),
                "open": len(open_s),
                "closed_winrate": wr, "sample_ok": len(closed) >= 30,
            }

        sigs_24h = [s for s in all_sigs if s.created_at and s.created_at >= cutoff_24h]
        sigs_7d  = [s for s in all_sigs if s.created_at and s.created_at >= cutoff_7d]
        sigs_30d = all_sigs

        closed_30d = [s for s in sigs_30d if s.status in ("TP1", "TP2", "TP3", "SL")]

        # Long vs Short
        def _side(sigs):
            wins = [s for s in sigs if s.status in WIN_ST]
            n = max(1, len(sigs))
            return {"total": len(sigs), "winrate": round(len(wins) / n * 100, 1)}

        long_30d  = [s for s in closed_30d if s.side == "LONG"]
        short_30d = [s for s in closed_30d if s.side == "SHORT"]

        # Best / Worst pairs
        sym_map: dict = _dd(list)
        for s in closed_30d:
            sym_map[s.symbol].append(s)

        pair_rows = []
        for sym, sigs in sym_map.items():
            wins = [s for s in sigs if s.status in WIN_ST]
            rrs  = [float(s.risk_reward or 0) for s in sigs]
            n = len(sigs)
            pair_rows.append({
                "symbol": sym, "total": n,
                "winrate": round(len(wins) / n * 100, 1),
                "avg_rr": round(sum(rrs) / max(1, n), 2),
            })

        best_pairs  = sorted(pair_rows, key=lambda x: x["winrate"], reverse=True)[:5]
        worst_pairs = sorted(pair_rows, key=lambda x: x["winrate"])[:5]

        # Confidence bands (30D all statuses)
        def _band(lo, hi):
            bsigs = [s for s in sigs_30d if lo <= float(s.confidence or 0) < hi]
            wins  = [s for s in bsigs if s.status in WIN_ST]
            losses = [s for s in bsigs if s.status == "SL"]
            n = len(bsigs)
            return {
                "signals": n, "wins": len(wins), "losses": len(losses),
                "winrate": round(len(wins) / n * 100, 1) if n >= 3 else None,
            }

        # Status distribution (30D)
        status_dist = {k: sum(1 for s in sigs_30d if s.status == k)
                       for k in ("OPEN", "TP1", "TP2", "SL", "EXPIRED")}

        total_closed_30d = len(closed_30d)

        result = {
            "sample_size": total_closed_30d,
            "data_collecting": total_closed_30d < 30,
            "period_24h": _period(sigs_24h),
            "period_7d":  _period(sigs_7d),
            "period_30d": _period(sigs_30d),
            "long_vs_short": {"long": _side(long_30d), "short": _side(short_30d)},
            "best_pairs":  best_pairs,
            "worst_pairs": worst_pairs,
            "confidence_bands": {
                "75_80":   _band(75, 80),
                "80_85":   _band(80, 85),
                "85_90":   _band(85, 90),
                "90_plus": _band(90, 200),
            },
            "status_distribution": status_dist,
            "updated_at": now.isoformat(),
        }
        _cache_set("perf_center", result)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Sprint 13: Market Radar ───────────────────────────────────────

@app.get("/api/public/market-radar")
async def api_market_radar():
    """Daily market intelligence: bias, risk, setups, sentiment."""
    cached = _cache_get("market_radar")
    if cached is not None:
        return JSONResponse(cached)

    from collections import defaultdict as _dd

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_2h  = now - timedelta(hours=2)

    WIN_ST = ("TP1", "TP2", "TP3")

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
                select(FundingRateSnapshot)
                .join(fund_subq,
                    (FundingRateSnapshot.symbol == fund_subq.c.symbol) &
                    (FundingRateSnapshot.created_at == fund_subq.c.latest))
            )
            funding_snaps = list(fund_res.scalars().all())

        def _bias(sigs):
            if not sigs:
                return "NEUTRAL"
            longs = sum(1 for s in sigs if s.side == "LONG")
            r = longs / len(sigs)
            return "BULLISH" if r >= 0.65 else ("BEARISH" if r <= 0.35 else "NEUTRAL")

        btc_sigs   = [s for s in recent_sigs if s.symbol == "BTCUSDT"]
        eth_sigs   = [s for s in recent_sigs if s.symbol == "ETHUSDT"]
        other_sigs = [s for s in recent_sigs if s.symbol not in ("BTCUSDT", "ETHUSDT")]

        extreme_pos = sum(1 for s in funding_snaps if s.classification == "extreme_positive")
        extreme_neg = sum(1 for s in funding_snaps if s.classification == "extreme_negative")
        total_fund  = max(1, len(funding_snaps))
        er = (extreme_pos + extreme_neg) / total_fund
        market_risk = "HIGH" if er >= 0.30 else ("MEDIUM" if er >= 0.12 else "LOW")

        strongest = [
            {
                "symbol": s.symbol, "side": s.side,
                "confidence": round(float(s.confidence or 0), 1),
                "rr": round(float(s.risk_reward or 0), 2),
                "status": s.status, "tf": s.timeframe,
            }
            for s in sorted(recent_sigs, key=lambda x: float(x.confidence or 0), reverse=True)[:10]
        ]

        total_sigs   = len(recent_sigs)
        long_count   = sum(1 for s in recent_sigs if s.side == "LONG")
        short_count  = total_sigs - long_count
        dir_score    = (long_count / max(1, total_sigs)) * 60
        fund_adj     = -((extreme_pos - extreme_neg) / total_fund) * 20
        sentiment_score = max(0, min(100, round(dir_score + 20 + fund_adj)))
        sentiment_label = "GREED" if sentiment_score >= 60 else ("FEAR" if sentiment_score <= 40 else "NEUTRAL")

        _SECTOR_MAP = {
            "Layer 1":  {"BTCUSDT","ETHUSDT","SOLUSDT","AVAXUSDT","DOTUSDT","NEARUSDT","ADAUSDT","TRXUSDT"},
            "DeFi":     {"UNIUSDT","AAVEUSDT","COMPUSDT","CRVUSDT","MKRUSDT","DYDXUSDT","SNXUSDT"},
            "Layer 2":  {"MATICUSDT","OPUSDT","ARBUSDT","STRKUSDT"},
            "Meme":     {"DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT","WIFUSDT"},
            "AI/Data":  {"FETUSDT","WLDUSDT","TAOUSDT","RENDERUSDT","OCEANUSDT"},
            "Gaming":   {"AXSUSDT","SANDUSDT","MANAUSDT","GALAUSDT","IMXUSDT"},
        }
        sector_stats = []
        for sector, syms in _SECTOR_MAP.items():
            ssigs = [s for s in recent_sigs if s.symbol in syms]
            if ssigs:
                longs = sum(1 for s in ssigs if s.side == "LONG")
                r = longs / len(ssigs)
                sector_stats.append({
                    "sector": sector, "signals": len(ssigs),
                    "bias": "BULLISH" if r >= 0.6 else ("BEARISH" if r <= 0.4 else "NEUTRAL"),
                })
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


# ── Sprint 14: Setup Library ──────────────────────────────────────

_SETUP_LIBRARY = [
    {
        "id": "trend_continuation",
        "name": "Trend Continuation",
        "description": "Enter in the direction of the dominant trend after a pullback, when momentum resumes.",
        "required_conditions": [
            "1D EMA50 above EMA200 (LONG) or below (SHORT)",
            "4H Break of Structure in trend direction",
            "1H pullback into OB or FVG zone",
            "15M entry trigger (BOS + volume confirm)",
        ],
        "invalidation": "Trend reversal candle closes below EMA200 on 4H. New Lower Low (LONG) or Higher High (SHORT).",
        "example": "BTCUSDT LONG: Daily trend is bullish. Price pulls back to 4H Order Block. 1H shows bullish engulfing. 15M BOS confirms. Entry zone: the OB level.",
        "risk_notes": "Size at 1-2% account risk. SL below the Order Block. TP1 at previous HH.",
        "status": "Active",
    },
    {
        "id": "pullback_entry",
        "name": "Pullback Entry",
        "description": "After a strong impulse move, price retraces to a key level before continuing.",
        "required_conditions": [
            "Clear impulse leg identified on 4H",
            "Retracement to 50-61.8% Fibonacci or Order Block",
            "1H structure holds (no CHoCH against trade direction)",
            "15M shows entry trigger",
        ],
        "invalidation": "Price breaks below pullback entry zone with volume. Structure fails on 1H.",
        "example": "ETHUSDT LONG: Strong 4H rally. Price pulls back 50% to OB zone. 1H forms higher low. 15M BOS up confirms entry.",
        "risk_notes": "Tighter SL possible (below the pullback low). Higher RR achievable.",
        "status": "Active",
    },
    {
        "id": "bos_retest",
        "name": "BOS Retest",
        "description": "After a Break of Structure, price often retests the broken level before continuing.",
        "required_conditions": [
            "Clean BOS identified on 4H (swing high or low broken)",
            "Price retests the broken level (now flipped support/resistance)",
            "1H shows rejection candle at retest zone",
            "15M entry confirmation",
        ],
        "invalidation": "Price closes back through the BOS level in opposite direction.",
        "example": "SOLUSDT LONG: 4H breaks above key resistance. Price retests that level. 1H pin bar at the retest. 15M BOS up is entry trigger.",
        "risk_notes": "SL below the BOS retest zone. Invalidation is clear if level fails.",
        "status": "Active",
    },
    {
        "id": "liquidity_sweep",
        "name": "Liquidity Sweep",
        "description": "Price sweeps liquidity (stop hunts) above swing highs or below swing lows, then reverses.",
        "required_conditions": [
            "Identified liquidity pool (cluster of equal highs/lows)",
            "Price sweeps the liquidity with a wick",
            "Immediate rejection and reversal candle",
            "4H and 1H structure supports the reversal direction",
        ],
        "invalidation": "Price continues in the sweep direction beyond the sweep candle body.",
        "example": "BTCUSDT SHORT: Multiple equal highs create liquidity. Price wicks above, then closes bearish. 4H structure bearish. 15M entry down.",
        "risk_notes": "SL above the sweep wick. High reward setups when caught correctly.",
        "status": "Active",
    },
    {
        "id": "funding_reversal",
        "name": "Funding Reversal",
        "description": "Extreme funding rates signal crowded positioning; counter-trend reversal setup.",
        "required_conditions": [
            "Funding rate classified as extreme_positive (>0.08%) or extreme_negative (<-0.08%)",
            "Price shows reversal structure on 4H",
            "1H confirms with setup conditions",
            "15M entry in reversal direction",
        ],
        "invalidation": "Funding normalizes without price reversal. Strong momentum continues.",
        "example": "Funding extremely positive (+0.12%). Market is overleveraged long. 4H shows bearish CHoCH. Setup triggers SHORT.",
        "risk_notes": "Use smaller size — funding reversals can be violent. Don't fight strong trends alone on funding.",
        "status": "Active",
    },
    {
        "id": "order_block_retest",
        "name": "Order Block Retest",
        "description": "Institution order blocks (candles before strong impulse moves) act as support/resistance on retest.",
        "required_conditions": [
            "4H Order Block identified (last candle before impulse)",
            "Price returns to the OB zone",
            "1H shows rejection or reversal at OB",
            "15M entry confirmation",
        ],
        "invalidation": "Price closes through the entire Order Block with follow-through.",
        "example": "SOLUSDT: Strong bullish impulse on 4H. Last bearish candle before impulse is OB. Price retests. 1H bullish, 15M BOS up.",
        "risk_notes": "OB midpoint or bottom (for bullish OB) as SL anchor.",
        "status": "Active",
    },
    {
        "id": "fvg_retest",
        "name": "FVG Retest",
        "description": "Fair Value Gaps (price imbalances) tend to get filled. Entry on retest of the FVG.",
        "required_conditions": [
            "Identified FVG on 4H or 1H (3-candle gap in price)",
            "Price retraces into the FVG zone",
            "Candle closes inside or at FVG boundary",
            "15M entry confirmation at FVG",
        ],
        "invalidation": "Price closes completely through the FVG with no reaction.",
        "example": "BTCUSDT: 4H bullish impulse leaves FVG. Price retraces to FVG. 1H bullish, volume dries up. 15M BOS up is entry.",
        "risk_notes": "50% of FVG as SL anchor. FVGs are strong magnetic levels.",
        "status": "Active",
    },
    {
        "id": "vwap_reclaim",
        "name": "VWAP Reclaim",
        "description": "Price reclaims VWAP after being rejected, signaling institutional re-entry.",
        "required_conditions": [
            "Price was below VWAP (for LONG) or above VWAP (for SHORT)",
            "Strong reclaim candle closes on the correct side of VWAP",
            "Volume above average on reclaim candle",
            "15M EMA alignment and entry trigger",
        ],
        "invalidation": "Price immediately fails back through VWAP after reclaim.",
        "example": "ETHUSDT: Price below VWAP, then bullish engulf reclaims VWAP with high volume. 15M BOS up confirms entry.",
        "risk_notes": "VWAP reclaim setups work best in trending sessions. Less reliable in low-volume periods.",
        "status": "Active",
    },
]


@app.get("/api/public/setup-library")
async def api_setup_library():
    """Educational setup library — trading concept explanations only."""
    return JSONResponse({"setups": _SETUP_LIBRARY, "count": len(_SETUP_LIBRARY)})


# ── Sprint 15: Public Watchlist ───────────────────────────────────

@app.get("/api/public/watchlist")
async def api_public_watchlist(symbols: str = ""):
    """Return latest signal + status for each requested symbol (localStorage watchlist)."""
    import re as _re
    raw = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
    symbol_list = [_re.sub(r"[^A-Z0-9]", "", s)[:20] for s in raw]
    symbol_list = [s for s in symbol_list if s]

    if not symbol_list:
        return JSONResponse({"error": "symbols parameter required"}, status_code=400)

    try:
        prices = ws_health().get("prices", {})
        rows = []

        async with SessionLocal() as session:
            for sym in symbol_list:
                res = await session.execute(
                    select(Signal)
                    .where(
                        Signal.symbol == sym,
                        Signal.strategy == _MTF_STRATEGY,
                        Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    )
                    .order_by(desc(Signal.created_at))
                    .limit(1)
                )
                sig = res.scalar_one_or_none()
                rows.append({
                    "symbol": sym,
                    "current_price": prices.get(sym),
                    "status": sig.status if sig else "no_signal",
                    "latest_signal": {
                        "side": sig.side,
                        "confidence": round(float(sig.confidence or 0), 1),
                        "rr": round(float(sig.risk_reward or 0), 2),
                        "status": sig.status,
                        "entry": round(float(sig.entry_low or 0), 6),
                        "tp1": round(float(sig.tp1 or 0), 6),
                        "sl": round(float(sig.stop_loss or 0), 6),
                        "timeframe": sig.timeframe,
                        "created_at": sig.created_at.isoformat() if sig.created_at else None,
                    } if sig else None,
                })

        return JSONResponse({"watchlist": rows, "count": len(rows)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Sprint 19A: Market Regime ─────────────────────────────────────

@app.get("/api/public/market-regime")
async def api_public_market_regime():
    """Current market regime classification and supporting metrics."""
    try:
        from app.market_data.market_regime import get_market_regime
        regime = await get_market_regime()
        if regime is None:
            return JSONResponse(
                {"error": "market regime not yet calculated — try again after the first scan cycle"},
                status_code=503,
            )
        return JSONResponse({
            "market_regime":   regime.market_regime,
            "regime_score":    regime.regime_score,
            "breadth":         regime.breadth_ema200,
            "breadth_ema50":   regime.breadth_ema50,
            "btc_trend":       regime.btc_trend,
            "eth_trend":       regime.eth_trend,
            "atr_percentile":  regime.atr_percentile,
            "calculated_at":   regime.calculated_at,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Sprint 19B: Short Protection Analytics ────────────────────────

@app.get("/api/public/short-protection")
async def api_public_short_protection():
    """Short protection filter statistics — rejection counts and top reasons."""
    try:
        from app.scanner.short_protection import get_short_protection_stats
        stats = await get_short_protection_stats()
        return JSONResponse(stats)
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


@app.get("/api/oi/status")
async def api_oi_status():
    """
    Sprint 11A — Open Interest dashboard cards.

    Returns counts of Bullish / Bearish / Neutral OI symbols based on the
    most recent OI snapshot per symbol recorded in the last 2 hours.
    Also returns the 20 most recent individual snapshots for detail display.
    """
    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

        async with SessionLocal() as session:
            # Most recent snapshot per symbol within the last 2h
            subq = (
                select(
                    OpenInterestSnapshot.symbol,
                    _sqlfunc.max(OpenInterestSnapshot.created_at).label("latest"),
                )
                .where(OpenInterestSnapshot.created_at >= cutoff)
                .group_by(OpenInterestSnapshot.symbol)
                .subquery()
            )
            res = await session.execute(
                select(OpenInterestSnapshot)
                .join(
                    subq,
                    (OpenInterestSnapshot.symbol == subq.c.symbol) &
                    (OpenInterestSnapshot.created_at == subq.c.latest),
                )
            )
            snapshots = res.scalars().all()

        bullish = sum(1 for s in snapshots if s.oi_score > 0)
        bearish = sum(1 for s in snapshots if s.oi_score < 0)
        neutral = sum(1 for s in snapshots if s.oi_score == 0)
        total   = len(snapshots)

        recent = sorted(snapshots, key=lambda s: s.created_at, reverse=True)[:20]

        return JSONResponse({
            "open_interest_status": "active" if total > 0 else "no_data",
            "total_symbols":  total,
            "bullish_oi":     bullish,
            "bearish_oi":     bearish,
            "neutral_oi":     neutral,
            "snapshots": [
                {
                    "symbol":        s.symbol,
                    "open_interest": s.open_interest,
                    "oi_change_5m":  s.oi_change_5m,
                    "oi_change_15m": s.oi_change_15m,
                    "oi_change_1h":  s.oi_change_1h,
                    "price_change":  s.price_change_pct,
                    "oi_score":      s.oi_score,
                    "time":          s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
                }
                for s in recent
            ],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/funding/status")
async def api_funding_status():
    """
    Sprint 11B — Funding Rate dashboard cards.

    Returns crowd-positioning counts (extreme_positive / extreme_negative /
    neutral) from the most recent funding snapshot per symbol in the last 2h.
    """
    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

        async with SessionLocal() as session:
            subq = (
                select(
                    FundingRateSnapshot.symbol,
                    _sqlfunc.max(FundingRateSnapshot.created_at).label("latest"),
                )
                .where(FundingRateSnapshot.created_at >= cutoff)
                .group_by(FundingRateSnapshot.symbol)
                .subquery()
            )
            res = await session.execute(
                select(FundingRateSnapshot)
                .join(
                    subq,
                    (FundingRateSnapshot.symbol == subq.c.symbol) &
                    (FundingRateSnapshot.created_at == subq.c.latest),
                )
            )
            snaps = res.scalars().all()

        extreme_pos = sum(1 for s in snaps if s.classification == "extreme_positive")
        extreme_neg = sum(1 for s in snaps if s.classification == "extreme_negative")
        neutral_cnt = sum(1 for s in snaps if s.classification == "neutral")
        positive_cnt = sum(1 for s in snaps if s.classification == "positive")
        negative_cnt = sum(1 for s in snaps if s.classification == "negative")
        total = len(snaps)

        recent = sorted(snaps, key=lambda s: s.created_at, reverse=True)[:20]

        return JSONResponse({
            "funding_status":          "active" if total > 0 else "no_data",
            "total_symbols":           total,
            "extreme_positive_funding": extreme_pos,
            "extreme_negative_funding": extreme_neg,
            "neutral_funding":          neutral_cnt,
            "positive_funding":         positive_cnt,
            "negative_funding":         negative_cnt,
            "snapshots": [
                {
                    "symbol":         s.symbol,
                    "funding_rate":   s.funding_rate,
                    "funding_pct":    round(s.funding_rate * 100, 5),
                    "classification": s.classification,
                    "time":           s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
                }
                for s in recent
            ],
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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


@app.get("/api/system/metrics")
async def api_system_metrics():
    """System-level signal metrics for monitoring and external integrations."""
    try:
        async with SessionLocal() as session:
            total_res = await session.execute(
                select(_sqlfunc.count(Signal.id))
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
            )
            signals_total = int(total_res.scalar() or 0)

            open_res = await session.execute(
                select(_sqlfunc.count(Signal.id))
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status == "OPEN",
                )
            )
            open_signals = int(open_res.scalar() or 0)

            closed_res = await session.execute(
                select(_sqlfunc.count(Signal.id))
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
                )
            )
            closed_signals = int(closed_res.scalar() or 0)

            wins_res = await session.execute(
                select(_sqlfunc.count(Signal.id))
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status.in_(["TP1", "TP2", "TP3"]),
                )
            )
            wins = int(wins_res.scalar() or 0)

        winrate_closed = round(wins / closed_signals * 100, 1) if closed_signals > 0 else None

        return JSONResponse({
            "ok": True,
            "signals_total": signals_total,
            "open_signals": open_signals,
            "closed_signals": closed_signals,
            "winrate_closed": winrate_closed,
            "universe": len(universe.symbols),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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
        f'<a href="{tg_url}" target="_blank" rel="noopener" class="nav-tg">Join Telegram</a>'
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
            f'Join Telegram Channel</a>'
        )
    if dc_url:
        hero_btns.append(
            f'<a href="{dc_url}" target="_blank" rel="noopener" class="btn-outline">'
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
    twitter_url = _safe_url(os.getenv("TWITTER_URL", ""))
    youtube_url = _safe_url(os.getenv("YOUTUBE_URL", ""))
    footer_comm = []
    if tg_url:
        footer_comm.append(f'<a href="{tg_url}" target="_blank" rel="noopener">Telegram</a>')
    if dc_url:
        footer_comm.append(f'<a href="{dc_url}" target="_blank" rel="noopener">Discord</a>')
    if twitter_url:
        footer_comm.append(f'<a href="{twitter_url}" target="_blank" rel="noopener">Twitter / X</a>')
    if youtube_url:
        footer_comm.append(f'<a href="{youtube_url}" target="_blank" rel="noopener">YouTube</a>')
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
    if not don_cards:
        for coin, net, netname, _addr, color in wallets:
            don_cards.append(
                f'<div class="don-card card disabled">'
                f'<div class="don-hdr"><span class="don-coin" style="color:{color}">{_esc(coin)} &mdash; {_esc(net)}</span>'
                f'<span class="don-net">{_esc(netname)}</span></div>'
                f'<div class="don-empty">Wallet address not configured yet</div>'
                f'</div>'
            )
    donate_section = (
        '<div class="sh">'
        '<div class="sh-lbl">&#9829; SUPPORT</div>'
        '<div class="sh-title">Support Alpha Radar Signals &#10084;&#65039;</div>'
        '<div class="sh-sub">If Alpha Radar Signals helps you, consider supporting development and server costs.</div>'
        '</div>'
        '<div class="don-intro">'
        '<div class="card" style="padding:20px">'
        '<div style="font-weight:900;font-size:18px;margin-bottom:8px;color:var(--text)">Keep Alpha Radar Free</div>'
        '<div class="don-copyline">Every donation helps fund data, servers, backtesting, and new features for the trading community.</div>'
        '<div style="margin-top:14px;color:var(--green);font-size:12px;font-weight:800">Thank you for your support! &#128591;</div>'
        '</div>'
        '<div class="don-grid">' + "".join(don_cards) + '</div>'
        '</div>'
    )
    html = html.replace("__DONATE__", donate_section)

    # ── exchange affiliate cards ─────────────────────────────────────
    aff_cards = []
    exchanges = [
        ("Binance", binance_aff, "#f3ba2f", "binance", "Best for Binance", "World's largest crypto exchange with deepest liquidity."),
        ("Bybit", bybit_aff, "#f7a600", "bybit", "Best Futures Platform", "Top derivatives & perpetual futures with low fees."),
        ("OKX", okx_aff, "#1a82ff", "okx", "Leading Altcoins", "Advanced trading tools and deep altcoin markets."),
        ("Bitget", bitget_aff, "#00e6b3", "bitget", "Best Copy Trading", "Follow top traders automatically with copy trading."),
    ]
    for name, url, color, logo, tag, desc in exchanges:
        safe_name = _esc(name)
        if url:
            btn = (
                f'<a href="{url}" target="_blank" rel="noopener" class="exch-btn" '
                f'style="background:{color};box-shadow:0 4px 14px {color}44">Register Now &rarr;</a>'
            )
            disabled_cls = ''
        else:
            btn = '<span class="exch-btn coming-soon">Coming Soon</span>'
            disabled_cls = ' disabled'
        aff_cards.append(
            f'<div class="exch-card card{disabled_cls}">'
            f'<div class="exch-ico"><img src="/static/exchanges/{logo}.svg" alt="{safe_name}" class="exch-logo-img"></div>'
            f'<div class="exch-name" style="color:{color}">{safe_name}</div>'
            f'<div class="exch-tag">{tag}</div>'
            f'<div class="exch-desc">{desc}</div>'
            f'{btn}'
            f'</div>'
        )
    aff_section = '<div class="exch-grid">' + "".join(aff_cards) + '</div>'
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


@app.get("/performance-center", response_class=HTMLResponse)
async def performance_center_page():
    return HTMLResponse(_performance_center_page_html())


@app.get("/market-radar", response_class=HTMLResponse)
async def market_radar_page():
    return HTMLResponse(_market_radar_page_html())


@app.get("/setup-library", response_class=HTMLResponse)
async def setup_library_page():
    return HTMLResponse(_setup_library_page_html())


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page():
    return HTMLResponse(_watchlist_page_html())


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
    <a href="/market-radar">Market Radar</a>
    <a href="/performance-center">Performance</a>
    <a href="/setup-library">Setup Library</a>
    <a href="/watchlist">Watchlist</a>
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


# ══════════════════════════════════════════════════════════════════
#  Sprint 12 — Performance Analytics Center
# ══════════════════════════════════════════════════════════════════

def _performance_center_page_html() -> str:
    css = """
.pc2-hdr{margin-bottom:22px}
.pc2-title{font-size:26px;font-weight:900;color:#eaf2ff;letter-spacing:.5px}
.pc2-sub{font-size:12px;color:#7fa0c8;margin-top:4px;letter-spacing:1px}
.pc2-warn{background:#1a140833;border:1px solid #ffd84d55;border-radius:9px;padding:10px 16px;margin-bottom:18px;font-size:12px;color:#ffd84d}
.pc2-tabs{display:flex;gap:8px;margin-bottom:18px}
.pc2-tab{padding:7px 18px;border-radius:8px;border:1px solid #17314b;background:transparent;color:#8fa8c7;cursor:pointer;font-size:12px;font-weight:700;transition:all .15s}
.pc2-tab.act{background:#08a98f22;border-color:#20f0c0;color:#20f0c0}
.pc2-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.pc2-card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:12px;padding:16px;text-align:center}
.pc2-lbl{font-size:9px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.pc2-val{font-size:24px;font-weight:900}
.pc2-2col{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}
.pc2-sec{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:12px;padding:18px}
.pc2-sec-title{font-size:10px;font-weight:900;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid #17314b}
.pc2-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #0e1e2e;font-size:13px}
.pc2-row:last-child{border-bottom:none}
.pc2-rlbl{color:#7fa0c8;font-size:12px}
.pc2-rval{font-weight:700;color:#eaf2ff}
.pc2-disclaimer{background:#0a1520;border:1px solid #17314b;border-radius:10px;padding:14px;font-size:11px;color:#627a99;margin-top:18px}
.pc2-collect{background:rgba(8,232,210,.07);border:1px solid rgba(8,232,210,.2);border-radius:12px;padding:28px;text-align:center;margin-bottom:18px}
.pc2-collect h3{color:#20f0c0;margin-bottom:8px}
.pc2-collect p{color:#7fa0c8;font-size:13px}
@media(max-width:860px){.pc2-grid{grid-template-columns:1fr 1fr}.pc2-2col{grid-template-columns:1fr}}
@media(max-width:480px){.pc2-grid{grid-template-columns:1fr}}
"""
    body = """
<div class="pc2-hdr">
  <div class="pc2-title">PERFORMANCE ANALYTICS CENTER</div>
  <div class="pc2-sub">MTF_SMC_STRICT ENGINE &nbsp;·&nbsp; 15M / 1H / 4H / 1D &nbsp;·&nbsp; Live Production Signals</div>
</div>

<div class="pc2-warn">
  ⚠&nbsp; Only MTF_SMC_STRICT signals on 15m–1D timeframes are included. Legacy signals are excluded.
  &nbsp;|&nbsp; <a href="/risk-disclaimer" style="color:#ffd84d">Risk Disclaimer</a>
</div>

<div class="pc2-tabs">
  <button class="pc2-tab act" onclick="swPeriod('30d',this)">30 Days</button>
  <button class="pc2-tab" onclick="swPeriod('7d',this)">7 Days</button>
  <button class="pc2-tab" onclick="swPeriod('24h',this)">24 Hours</button>
</div>

<div id="collect-msg" class="pc2-collect" style="display:none">
  <h3>Collecting Verified Performance Data</h3>
  <p>Alpha Radar requires at least 30 closed signals before showing headline statistics. Check back as more signals close.</p>
</div>

<div id="main-data">
  <div class="pc2-grid">
    <div class="pc2-card"><div class="pc2-lbl">Total Signals</div><div id="pc-total" class="pc2-val" style="color:#eaf2ff">—</div></div>
    <div class="pc2-card"><div class="pc2-lbl">Closed</div><div id="pc-closed" class="pc2-val" style="color:#20e6c3">—</div></div>
    <div class="pc2-card"><div class="pc2-lbl">Win Rate</div><div id="pc-wr" class="pc2-val g">—</div></div>
    <div class="pc2-card"><div class="pc2-lbl">Open</div><div id="pc-open" class="pc2-val" style="color:#20a7ff">—</div></div>
    <div class="pc2-card"><div class="pc2-lbl">TP1 Hit</div><div id="pc-tp1" class="pc2-val g">—</div></div>
    <div class="pc2-card"><div class="pc2-lbl">TP2 Hit</div><div id="pc-tp2" class="pc2-val g">—</div></div>
    <div class="pc2-card"><div class="pc2-lbl">SL Hit</div><div id="pc-sl" class="pc2-val r">—</div></div>
    <div class="pc2-card"><div class="pc2-lbl">Expired</div><div id="pc-exp" class="pc2-val y">—</div></div>
  </div>

  <div class="pc2-2col">
    <!-- Long vs Short -->
    <div class="pc2-sec">
      <div class="pc2-sec-title">Long vs Short (30D Closed)</div>
      <div class="pc2-row"><span class="pc2-rlbl">LONG Signals</span><span id="ls-long-tot" class="pc2-rval g">—</span></div>
      <div class="pc2-row"><span class="pc2-rlbl">LONG Win Rate</span><span id="ls-long-wr" class="pc2-rval g">—</span></div>
      <div class="pc2-row"><span class="pc2-rlbl">SHORT Signals</span><span id="ls-short-tot" class="pc2-rval r">—</span></div>
      <div class="pc2-row"><span class="pc2-rlbl">SHORT Win Rate</span><span id="ls-short-wr" class="pc2-rval r">—</span></div>
    </div>
    <!-- Status Distribution -->
    <div class="pc2-sec">
      <div class="pc2-sec-title">Status Distribution (30D)</div>
      <div id="status-dist"></div>
    </div>
  </div>

  <div class="pc2-2col">
    <!-- Best Pairs -->
    <div class="pc2-sec">
      <div class="pc2-sec-title">Best Pairs (30D)</div>
      <div id="best-pairs"></div>
    </div>
    <!-- Worst Pairs -->
    <div class="pc2-sec">
      <div class="pc2-sec-title">Worst Pairs (30D)</div>
      <div id="worst-pairs"></div>
    </div>
  </div>

  <!-- Confidence Bands -->
  <div class="pc2-sec" style="margin-bottom:18px">
    <div class="pc2-sec-title">Confidence Bands (30D)</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px" id="conf-bands"></div>
  </div>
</div>

<div class="pc2-disclaimer">
  ⚠ Risk Disclaimer: Past performance does not guarantee future results. All signals are for educational purposes only.
  Trading crypto futures involves substantial risk of loss.
  <a href="/risk-disclaimer" style="color:#20e6c3">Read full disclaimer →</a>
</div>
"""
    js = """
let _perfData = null;
let _curPeriod = '30d';

function swPeriod(p, btn) {
  _curPeriod = p;
  document.querySelectorAll('.pc2-tab').forEach(b => b.classList.remove('act'));
  btn.classList.add('act');
  renderPeriod();
}

function renderPeriod() {
  if (!_perfData) return;
  const pd = _perfData['period_' + _curPeriod];
  if (!pd) return;
  document.getElementById('pc-total').textContent = pd.total;
  document.getElementById('pc-closed').textContent = pd.closed;
  document.getElementById('pc-open').textContent = pd.open;
  document.getElementById('pc-tp1').textContent = pd.tp1;
  document.getElementById('pc-tp2').textContent = pd.tp2;
  document.getElementById('pc-sl').textContent = pd.sl;
  document.getElementById('pc-exp').textContent = pd.expired;
  const wrEl = document.getElementById('pc-wr');
  if (pd.closed_winrate !== null && pd.closed_winrate !== undefined) {
    wrEl.textContent = pd.closed_winrate + '%';
    wrEl.className = 'pc2-val ' + (pd.closed_winrate >= 50 ? 'g' : 'r');
  } else {
    wrEl.textContent = '—';
    wrEl.className = 'pc2-val';
  }
}

function renderPairs(id, rows) {
  const el = document.getElementById(id);
  if (!rows || !rows.length) { el.innerHTML = '<div style="color:#627a99;font-size:13px;padding:10px">Not enough data</div>'; return; }
  el.innerHTML = rows.map(r =>
    '<div class="pc2-row"><span class="pc2-rlbl"><b>' + r.symbol + '</b> (' + r.total + ' sigs)</span>' +
    '<span class="pc2-rval ' + (r.winrate >= 50 ? 'g' : 'r') + '">' + r.winrate + '%</span></div>'
  ).join('');
}

async function load() {
  try {
    const r = await fetch('/api/public/performance-center');
    if (!r.ok) return;
    _perfData = await r.json();

    const dc = _perfData.data_collecting;
    document.getElementById('collect-msg').style.display = dc ? 'block' : 'none';
    document.getElementById('main-data').style.display = dc ? 'none' : 'block';
    if (dc) return;

    renderPeriod();

    // Long vs Short
    const ls = _perfData.long_vs_short;
    document.getElementById('ls-long-tot').textContent = ls.long.total;
    document.getElementById('ls-long-wr').textContent = ls.long.winrate + '%';
    document.getElementById('ls-short-tot').textContent = ls.short.total;
    document.getElementById('ls-short-wr').textContent = ls.short.winrate + '%';

    // Status distribution
    const sd = _perfData.status_distribution;
    const sdEl = document.getElementById('status-dist');
    sdEl.innerHTML = Object.entries(sd).map(([k, v]) =>
      '<div class="pc2-row"><span class="pc2-rlbl">' + k + '</span><span class="pc2-rval">' + v + '</span></div>'
    ).join('');

    // Pairs
    renderPairs('best-pairs', _perfData.best_pairs);
    renderPairs('worst-pairs', _perfData.worst_pairs);

    // Confidence bands
    const cb = _perfData.confidence_bands;
    const cbLabels = {'75_80':'75–80%','80_85':'80–85%','85_90':'85–90%','90_plus':'90%+'};
    const cbEl = document.getElementById('conf-bands');
    cbEl.innerHTML = Object.entries(cb).map(([k, v]) =>
      '<div class="pc2-card">' +
      '<div class="pc2-lbl">' + (cbLabels[k] || k) + '</div>' +
      '<div class="pc2-val" style="font-size:18px">' + v.signals + ' sigs</div>' +
      '<div style="font-size:11px;margin-top:4px;color:#20ff80">' + (v.winrate !== null ? v.winrate + '% wr' : '—') + '</div>' +
      '<div style="font-size:10px;color:#627a99">' + v.wins + 'W / ' + v.losses + 'L</div>' +
      '</div>'
    ).join('');
  } catch(e) { console.error(e); }
}

document.addEventListener('DOMContentLoaded', () => { load(); setInterval(load, 60000); });
"""
    return _page_shell("Performance Analytics Center", body, extra_css=css, extra_js=js)


# ══════════════════════════════════════════════════════════════════
#  Sprint 13 — Market Radar
# ══════════════════════════════════════════════════════════════════

def _market_radar_page_html() -> str:
    css = """
.mr-title{font-size:26px;font-weight:900;color:#eaf2ff;margin-bottom:4px}
.mr-sub{font-size:12px;color:#7fa0c8;margin-bottom:20px;letter-spacing:1px}
.mr-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}
.mr-card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:18px}
.mr-card-title{font-size:10px;font-weight:900;letter-spacing:2px;text-transform:uppercase;color:#7fa0c8;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid #17314b}
.mr-bias{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #0e1e2e;font-size:13px}
.mr-bias:last-child{border-bottom:none}
.mr-lbl{color:#7fa0c8;font-size:12px}
.bias-bull{color:#20ff80;font-weight:900}
.bias-bear{color:#ff4f61;font-weight:900}
.bias-neut{color:#ffd84d;font-weight:900}
.risk-low{color:#20ff80;font-size:28px;font-weight:900}
.risk-med{color:#ffd84d;font-size:28px;font-weight:900}
.risk-high{color:#ff4f61;font-size:28px;font-weight:900}
.mr-sentiment{text-align:center;padding:10px 0}
.mr-score{font-size:48px;font-weight:900;margin:8px 0}
.mr-sentiment-bar{height:10px;background:#0b1320;border-radius:999px;overflow:hidden;margin:8px 0}
.mr-sentiment-fill{height:100%;background:linear-gradient(90deg,#ff4f61,#ffd84d,#20ff80);border-radius:999px;transition:width .4s}
.mr-3col{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}
.mr-setup-row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #0e1e2e;font-size:13px}
.mr-setup-row:last-child{border-bottom:none}
.mr-24h{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:18px}
.mr-24h-card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:12px;padding:14px;text-align:center}
.mr-24h-lbl{font-size:9px;color:#7fa0c8;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.mr-24h-val{font-size:22px;font-weight:900}
@media(max-width:860px){.mr-grid{grid-template-columns:1fr 1fr}.mr-3col{grid-template-columns:1fr}.mr-24h{grid-template-columns:1fr 1fr}}
@media(max-width:480px){.mr-grid{grid-template-columns:1fr}.mr-24h{grid-template-columns:1fr}}
"""
    body = """
<div class="mr-title">TODAY'S MARKET RADAR</div>
<div class="mr-sub">LIVE MARKET INTELLIGENCE &nbsp;·&nbsp; Updated every 45 seconds</div>

<div class="mr-24h">
  <div class="mr-24h-card"><div class="mr-24h-lbl">Signals (24H)</div><div id="mr-sig24h" class="mr-24h-val" style="color:#20e6c3">—</div></div>
  <div class="mr-24h-card"><div class="mr-24h-lbl">Long Signals</div><div id="mr-long24h" class="mr-24h-val g">—</div></div>
  <div class="mr-24h-card"><div class="mr-24h-lbl">Short Signals</div><div id="mr-short24h" class="mr-24h-val r">—</div></div>
</div>

<div class="mr-grid">
  <!-- Market Bias -->
  <div class="mr-card">
    <div class="mr-card-title">Market Bias</div>
    <div class="mr-bias"><span class="mr-lbl">BTC</span><span id="mr-btc" class="bias-neut">—</span></div>
    <div class="mr-bias"><span class="mr-lbl">ETH</span><span id="mr-eth" class="bias-neut">—</span></div>
    <div class="mr-bias"><span class="mr-lbl">Altcoins</span><span id="mr-alt" class="bias-neut">—</span></div>
  </div>
  <!-- Market Risk -->
  <div class="mr-card" style="text-align:center">
    <div class="mr-card-title">Market Risk</div>
    <div id="mr-risk" class="risk-med">—</div>
    <div style="font-size:11px;color:#7fa0c8;margin-top:8px">Based on funding rate extremes</div>
  </div>
  <!-- Futures Sentiment -->
  <div class="mr-card">
    <div class="mr-card-title">Futures Sentiment</div>
    <div class="mr-sentiment">
      <div style="font-size:11px;color:#7fa0c8">Fear ← → Greed</div>
      <div id="mr-sentiment-score" class="mr-score" style="color:#ffd84d">—</div>
      <div class="mr-sentiment-bar"><div id="mr-sentiment-fill" class="mr-sentiment-fill" style="width:50%"></div></div>
      <div id="mr-sentiment-lbl" style="font-weight:900;font-size:14px;color:#ffd84d">—</div>
    </div>
  </div>
</div>

<div class="mr-3col">
  <!-- Strongest Setups -->
  <div class="mr-card" style="grid-column:1/-1">
    <div class="mr-card-title">Strongest Setups Today</div>
    <div id="mr-setups">
      <div style="color:#627a99;padding:16px;text-align:center">Loading...</div>
    </div>
  </div>
</div>

<!-- Sector Radar -->
<div class="mr-card" style="margin-bottom:18px">
  <div class="mr-card-title">Sector Radar</div>
  <div id="mr-sectors"></div>
</div>
"""
    js = """
function biasCls(b) {
  if(b==='BULLISH') return 'bias-bull';
  if(b==='BEARISH') return 'bias-bear';
  return 'bias-neut';
}
function riskCls(r) {
  if(r==='HIGH') return 'risk-high';
  if(r==='LOW') return 'risk-low';
  return 'risk-med';
}

async function loadRadar() {
  try {
    const r = await fetch('/api/public/market-radar');
    if (!r.ok) return;
    const d = await r.json();

    document.getElementById('mr-sig24h').textContent = d.signals_24h || 0;
    document.getElementById('mr-long24h').textContent = d.long_count_24h || 0;
    document.getElementById('mr-short24h').textContent = d.short_count_24h || 0;

    const mb = d.market_bias || {};
    ['btc','eth','alt'].forEach((id, i) => {
      const key = ['btc','eth','altcoin'][i];
      const el = document.getElementById('mr-' + id);
      el.textContent = mb[key] || 'NEUTRAL';
      el.className = biasCls(mb[key]);
    });

    const rEl = document.getElementById('mr-risk');
    rEl.textContent = d.market_risk || '—';
    rEl.className = riskCls(d.market_risk);

    const sent = d.futures_sentiment || {};
    document.getElementById('mr-sentiment-score').textContent = sent.score ?? '—';
    document.getElementById('mr-sentiment-lbl').textContent = sent.label || '—';
    const fill = document.getElementById('mr-sentiment-fill');
    fill.style.width = (sent.score || 50) + '%';
    const sc = sent.score || 50;
    document.getElementById('mr-sentiment-score').style.color = sc>=60?'#20ff80':(sc<=40?'#ff4f61':'#ffd84d');
    document.getElementById('mr-sentiment-lbl').style.color = sc>=60?'#20ff80':(sc<=40?'#ff4f61':'#ffd84d');

    const setups = d.strongest_setups || [];
    const sEl = document.getElementById('mr-setups');
    if (!setups.length) {
      sEl.innerHTML = '<div style="color:#627a99;padding:16px;text-align:center">No signals in last 24 hours</div>';
    } else {
      sEl.innerHTML = setups.map(s =>
        '<div class="mr-setup-row">' +
        '<span><b>' + s.symbol + '</b> <span style="color:#7fa0c8;font-size:11px">' + s.tf + '</span></span>' +
        '<span><span class="' + (s.side==='LONG'?'bl2':'bs2') + '">' + s.side + '</span></span>' +
        '<span style="color:#20e6c3">' + s.confidence + '%</span>' +
        '<span style="color:#ffd84d">1:' + s.rr + '</span>' +
        '<span class="' + (s.status==='OPEN'?'bopen':s.status==='SL'?'bsl':'btp') + '">' + s.status + '</span>' +
        '</div>'
      ).join('');
    }

    const sectors = d.sector_radar || [];
    const secEl = document.getElementById('mr-sectors');
    secEl.innerHTML = sectors.length
      ? '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px">' +
        sectors.map(s =>
          '<div style="background:#0a111a;border:1px solid #0e1e2e;border-radius:10px;padding:12px">' +
          '<div style="font-size:12px;font-weight:700;color:#eaf2ff">' + s.sector + '</div>' +
          '<div class="' + biasCls(s.bias) + '" style="font-size:11px;margin-top:5px">' + s.bias + ' (' + s.signals + ' sigs)</div>' +
          '</div>'
        ).join('') + '</div>'
      : '<div style="color:#627a99;padding:10px">Sector data collecting</div>';

  } catch(e) { console.error(e); }
}

document.addEventListener('DOMContentLoaded', () => { loadRadar(); setInterval(loadRadar, 45000); });
"""
    return _page_shell("Market Radar", body, extra_css=css, extra_js=js)


# ══════════════════════════════════════════════════════════════════
#  Sprint 14 — Setup Library
# ══════════════════════════════════════════════════════════════════

def _setup_library_page_html() -> str:
    css = """
.sl-title{font-size:26px;font-weight:900;color:#eaf2ff;margin-bottom:4px}
.sl-sub{font-size:12px;color:#7fa0c8;margin-bottom:20px;letter-spacing:1px}
.sl-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
.sl-card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:14px;padding:22px;position:relative;overflow:hidden}
.sl-card:before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,#08a98f,#20f0c0)}
.sl-card-name{font-size:16px;font-weight:900;color:#eaf2ff;margin-bottom:8px}
.sl-card-desc{font-size:13px;color:#8fa8c7;margin-bottom:14px;line-height:1.6}
.sl-section{margin-bottom:12px}
.sl-section-lbl{font-size:9px;font-weight:900;letter-spacing:2px;text-transform:uppercase;color:#20f0c0;margin-bottom:6px}
.sl-item{font-size:12px;color:#c9d8e8;padding:3px 0;display:flex;gap:8px;align-items:flex-start}
.sl-dot{width:5px;height:5px;border-radius:50%;background:#20f0c0;margin-top:5px;flex-shrink:0}
.sl-status{display:inline-flex;padding:3px 10px;border-radius:5px;font-size:10px;font-weight:900;letter-spacing:1px;margin-top:12px}
.sl-active{background:rgba(32,255,128,.12);color:#20ff80;border:1px solid rgba(32,255,128,.3)}
.sl-diag{background:rgba(255,216,77,.12);color:#ffd84d;border:1px solid rgba(255,216,77,.3)}
.sl-plan{background:rgba(32,167,255,.12);color:#20a7ff;border:1px solid rgba(32,167,255,.3)}
.sl-example{background:#061522;border:1px solid #17314b;border-radius:8px;padding:12px;font-size:11px;color:#7fa0c8;margin-top:10px;font-style:italic;line-height:1.6}
.sl-risk{font-size:11px;color:#ffd84d;margin-top:8px;background:rgba(255,216,77,.06);border:1px solid rgba(255,216,77,.18);border-radius:7px;padding:8px 12px}
@media(max-width:860px){.sl-grid{grid-template-columns:1fr}}
"""
    body = """
<div class="sl-title">SETUP LIBRARY</div>
<div class="sl-sub">EDUCATIONAL STRATEGY GUIDE &nbsp;·&nbsp; Trading concept explanations — no private source code exposed</div>
<div id="sl-grid" class="sl-grid">
  <div style="color:#627a99;padding:20px">Loading setup library...</div>
</div>
"""
    js = """
function statusCls(s) {
  if (s === 'Active') return 'sl-active';
  if (s === 'Diagnostic') return 'sl-diag';
  return 'sl-plan';
}

async function loadLibrary() {
  try {
    const r = await fetch('/api/public/setup-library');
    if (!r.ok) return;
    const d = await r.json();
    const grid = document.getElementById('sl-grid');
    grid.innerHTML = (d.setups || []).map(s =>
      '<div class="sl-card">' +
      '<div class="sl-card-name">' + s.name + '</div>' +
      '<div class="sl-card-desc">' + s.description + '</div>' +
      '<div class="sl-section">' +
      '<div class="sl-section-lbl">Required Conditions</div>' +
      s.required_conditions.map(c => '<div class="sl-item"><div class="sl-dot"></div><span>' + c + '</span></div>').join('') +
      '</div>' +
      '<div class="sl-section">' +
      '<div class="sl-section-lbl">Invalidation</div>' +
      '<div class="sl-item"><div class="sl-dot" style="background:#ff4f61"></div><span style="color:#ff9aaa">' + s.invalidation + '</span></div>' +
      '</div>' +
      '<div class="sl-example">📖 Example: ' + s.example + '</div>' +
      '<div class="sl-risk">⚠ Risk: ' + s.risk_notes + '</div>' +
      '<span class="sl-status ' + statusCls(s.status) + '">' + s.status + '</span>' +
      '</div>'
    ).join('');
  } catch(e) { console.error(e); }
}

document.addEventListener('DOMContentLoaded', loadLibrary);
"""
    return _page_shell("Setup Library", body, extra_css=css, extra_js=js)


# ══════════════════════════════════════════════════════════════════
#  Sprint 15 — Watchlist
# ══════════════════════════════════════════════════════════════════

def _watchlist_page_html() -> str:
    css = """
.wl-title{font-size:26px;font-weight:900;color:#eaf2ff;margin-bottom:4px}
.wl-sub{font-size:12px;color:#7fa0c8;margin-bottom:20px;letter-spacing:1px}
.wl-add{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}
.wl-input{flex:1;min-width:180px;background:#07101a;border:1px solid #17314b;border-radius:9px;color:#eaf2ff;padding:11px 14px;font-size:14px;outline:none;font-family:inherit}
.wl-input:focus{border-color:#20f0c0}
.wl-add-btn{background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;border:none;border-radius:9px;font-weight:900;font-size:13px;padding:11px 22px;cursor:pointer;letter-spacing:1px;white-space:nowrap}
.wl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-bottom:24px}
.wl-card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:18px;position:relative}
.wl-card-sym{font-size:16px;font-weight:900;color:#eaf2ff;margin-bottom:4px}
.wl-card-price{font-size:13px;color:#7fa0c8;margin-bottom:12px}
.wl-card-sig{font-size:12px;margin-bottom:10px}
.wl-remove{position:absolute;top:14px;right:14px;background:rgba(255,79,97,.1);border:1px solid rgba(255,79,97,.25);border-radius:6px;color:#ff4f61;padding:4px 9px;cursor:pointer;font-size:11px;font-weight:700}
.wl-remove:hover{background:rgba(255,79,97,.2)}
.wl-tg-cta{background:rgba(32,230,195,.06);border:1px solid rgba(32,230,195,.2);border-radius:13px;padding:20px;text-align:center;margin-bottom:18px}
.wl-tg-cta h3{color:#20e6c3;margin-bottom:8px;font-size:16px}
.wl-tg-cta p{color:#7fa0c8;font-size:13px;margin-bottom:12px}
.wl-empty{background:#0b1320;border:1px solid #17314b;border-radius:13px;padding:40px;text-align:center;color:#627a99}
.wl-empty h3{font-size:18px;margin-bottom:8px;color:#8fa8c7}
.no-sig{color:#627a99;font-size:12px;font-style:italic}
@media(max-width:480px){.wl-add{flex-direction:column}}
"""
    body = """
<div class="wl-title">MY WATCHLIST</div>
<div class="wl-sub">TRACK FAVORITE PAIRS &nbsp;·&nbsp; Stored locally in your browser &nbsp;·&nbsp; No login required</div>

<div class="wl-add">
  <input id="wl-input" class="wl-input" placeholder="Add symbol e.g. BTCUSDT, ETHUSDT" maxlength="30" onkeydown="if(event.key==='Enter')addSymbol()">
  <button class="wl-add-btn" onclick="addSymbol()">+ Add Symbol</button>
</div>

<div id="wl-grid" class="wl-grid">
  <div class="wl-empty"><h3>Your watchlist is empty</h3><p>Add symbols above to track their latest signals.</p></div>
</div>

<div class="wl-tg-cta">
  <h3>Get Notified on Telegram</h3>
  <p>Join our Telegram channel to receive real-time alerts when new signals appear for your watched pairs.</p>
  <a href="/faq" style="background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;border:none;border-radius:9px;font-weight:900;font-size:13px;padding:10px 22px;display:inline-block">Notify me on Telegram →</a>
</div>
"""
    js = """
const WL_KEY = 'alpha_radar_watchlist';

function loadWatchlist() {
  try { return JSON.parse(localStorage.getItem(WL_KEY) || '[]'); }
  catch(e) { return []; }
}

function saveWatchlist(list) {
  localStorage.setItem(WL_KEY, JSON.stringify(list));
}

function addSymbol() {
  const raw = document.getElementById('wl-input').value.trim().toUpperCase().replace(/[^A-Z0-9]/g,'');
  if (!raw || raw.length > 20) return;
  const list = loadWatchlist();
  if (list.includes(raw)) { alert(raw + ' is already in your watchlist'); return; }
  list.push(raw);
  saveWatchlist(list);
  document.getElementById('wl-input').value = '';
  renderAll();
}

function removeSymbol(sym) {
  const list = loadWatchlist().filter(s => s !== sym);
  saveWatchlist(list);
  renderAll();
}

function statusBadge(s) {
  if (!s || s === 'no_signal') return '<span style="color:#627a99;font-size:11px">No signal</span>';
  if (s === 'OPEN') return '<span class="bopen">OPEN</span>';
  if (s === 'SL') return '<span class="bsl">SL</span>';
  if (s === 'EXPIRED') return '<span class="bexp">EXP</span>';
  return '<span class="btp">' + s + '</span>';
}

function sideBadge(s) {
  return '<span class="' + (s==='LONG'?'bl2':'bs2') + '">' + s + '</span>';
}

async function renderAll() {
  const list = loadWatchlist();
  const grid = document.getElementById('wl-grid');
  if (!list.length) {
    grid.innerHTML = '<div class="wl-empty"><h3>Your watchlist is empty</h3><p>Add symbols above to track their latest signals.</p></div>';
    return;
  }
  grid.innerHTML = list.map(sym =>
    '<div class="wl-card" id="wl-' + sym + '">' +
    '<div class="wl-card-sym">' + sym + '</div>' +
    '<div class="wl-card-price" id="wlp-' + sym + '">Loading...</div>' +
    '<div class="wl-card-sig" id="wls-' + sym + '">Loading signal...</div>' +
    '<button class="wl-remove" onclick="removeSymbol(\'' + sym + '\')">✕ Remove</button>' +
    '</div>'
  ).join('');

  try {
    const r = await fetch('/api/public/watchlist?symbols=' + list.join(','));
    if (!r.ok) return;
    const d = await r.json();
    (d.watchlist || []).forEach(item => {
      const priceEl = document.getElementById('wlp-' + item.symbol);
      const sigEl = document.getElementById('wls-' + item.symbol);
      if (priceEl) {
        priceEl.textContent = item.current_price ? 'Price: $' + parseFloat(item.current_price).toFixed(4) : 'Price: —';
      }
      if (sigEl) {
        const sig = item.latest_signal;
        if (!sig) {
          sigEl.innerHTML = '<span class="no-sig">No signals found</span>';
        } else {
          sigEl.innerHTML =
            sideBadge(sig.side) + ' ' + statusBadge(sig.status) +
            ' <span style="color:#20e6c3">' + sig.confidence + '%</span>' +
            ' <span style="color:#ffd84d">1:' + sig.rr + '</span>' +
            '<div style="font-size:10px;color:#627a99;margin-top:4px">Entry: ' + sig.entry + ' | TP1: ' + sig.tp1 + '</div>';
        }
      }
    });
  } catch(e) { console.error(e); }
}

document.addEventListener('DOMContentLoaded', renderAll);
"""
    return _page_shell("Watchlist", body, extra_css=css, extra_js=js)


def create_app():
    # Sprint 20A — mount the multi-user auth API only when feature-flagged on.
    if settings.auth_enabled:
        try:
            from app.auth import setup_auth
            setup_auth(app)
        except Exception as exc:  # noqa: BLE001
            print(f"auth setup skipped (non-fatal): {exc!r}")
    # Sprint 20B — mount the per-user paper-trading API (needs auth to be usable).
    if settings.paper_trading_enabled:
        try:
            from app.paper_engine import setup_paper
            setup_paper(app)
        except Exception as exc:  # noqa: BLE001
            print(f"paper setup skipped (non-fatal): {exc!r}")
    # Sprint 20C — mount the encrypted exchange-credential vault API.
    if settings.exchange_api_vault_enabled:
        try:
            from app.exchange_vault import setup_exchange_vault
            setup_exchange_vault(app)
        except Exception as exc:  # noqa: BLE001
            print(f"exchange vault setup skipped (non-fatal): {exc!r}")
    # Sprint 20D — mount the demo auto-trading config/status API.
    if settings.auto_trade_demo_enabled:
        try:
            from app.auto_engine import setup_auto_engine
            setup_auto_engine(app)
        except Exception as exc:  # noqa: BLE001
            print(f"auto engine setup skipped (non-fatal): {exc!r}")
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
<title>ALPHA RADAR SIGNALS — AI-Powered Futures Signals</title>
<meta name="description" content="AI-powered Binance Futures signals with multi-timeframe analysis, risk-managed entries, live stats, Telegram alerts, affiliate exchanges and donation support."/>
<meta property="og:title" content="ALPHA RADAR SIGNALS — AI-Powered Futures Signals"/>
<meta property="og:description" content="Multi-Timeframe Analysis · Risk Managed · 24/7 Scanner · Free on Telegram"/>
<meta property="og:type" content="website"/>
<meta name="twitter:card" content="summary_large_image"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet" crossorigin/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
:root{--bg:#030b17;--panel:#071426;--card:rgba(8,18,36,.88);--card2:rgba(6,15,30,.76);--line:rgba(0,245,195,.16);--line2:rgba(0,245,195,.35);--cyan:#08e8d2;--green:#18f28b;--lime:#24ff8a;--red:#ff4564;--blue:#20a7ff;--yellow:#f2c94c;--text:#f3f8ff;--muted:#8ca9c4;--dim:#4d6c88;--shadow:0 0 35px rgba(0,245,195,.13);--r:18px}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Arial,sans-serif;line-height:1.55;overflow-x:hidden;background-image:radial-gradient(900px 500px at 70% 7%,rgba(8,232,210,.09),transparent 65%),radial-gradient(700px 420px at 15% 14%,rgba(24,242,139,.06),transparent 65%),linear-gradient(180deg,#031020 0%,#030b17 60%,#020813 100%)}
a{color:inherit;text-decoration:none}button{font-family:inherit}.container{width:min(1160px,calc(100% - 64px));margin:0 auto}.card{background:linear-gradient(180deg,rgba(9,22,43,.86),rgba(5,14,30,.86));border:1px solid rgba(49,159,208,.22);border-radius:var(--r);box-shadow:0 16px 48px rgba(0,0,0,.32),inset 0 1px 0 rgba(255,255,255,.03)}
/* NAV */
.nav{position:sticky;top:0;z-index:50;background:rgba(3,11,23,.74);backdrop-filter:blur(18px);border-bottom:1px solid rgba(255,255,255,.06)}.nav-in{height:74px;display:flex;align-items:center;justify-content:space-between;gap:28px}.brand{display:flex;align-items:center;gap:13px}.logo-svg{width:67px;height:67px;filter:drop-shadow(0 0 26px rgba(24,242,139,.55)) drop-shadow(0 0 8px rgba(8,232,210,.3))}.brand-word{font-weight:900;font-size:22px;letter-spacing:.9px;line-height:1.02}.brand-word b{display:block;color:var(--lime);font-size:15px;letter-spacing:3px}.nav-links{display:flex;align-items:center;gap:28px;margin-left:auto}.nav-links a{font-size:14px;font-weight:650;color:#d8e7f5}.nav-links a:hover{color:var(--green)}.nav-tg{display:inline-flex!important;align-items:center;gap:9px;background:linear-gradient(135deg,#10ddb6,#18f28b);color:#031117!important;border-radius:9px;padding:12px 22px;font-weight:900;box-shadow:0 10px 28px rgba(24,242,139,.24)}.nav-tg:before{content:'➤';font-size:14px}.nav-dc,.nav-admin,.live-pill{display:none!important}
/* HERO */
.hero{position:relative;min-height:610px;padding:66px 0 34px;overflow:hidden}.hero:before{content:'';position:absolute;inset:0;background:linear-gradient(90deg,rgba(3,11,23,.92) 0%,rgba(3,11,23,.62) 48%,rgba(3,11,23,.86) 100%),repeating-linear-gradient(90deg,rgba(255,255,255,.025) 0 1px,transparent 1px 68px),repeating-linear-gradient(0deg,rgba(255,255,255,.022) 0 1px,transparent 1px 68px);pointer-events:none}.hero:after{content:'';position:absolute;right:-7%;top:40px;width:60%;height:420px;opacity:.23;background:linear-gradient(160deg,transparent 8%,rgba(24,242,139,.08) 9%,transparent 10%),linear-gradient(40deg,transparent 20%,rgba(24,242,139,.08) 21%,transparent 22%);clip-path:polygon(0 45%,8% 46%,12% 42%,17% 55%,22% 50%,28% 52%,34% 34%,40% 37%,45% 28%,52% 46%,59% 43%,65% 56%,74% 49%,83% 52%,100% 44%,100% 100%,0 100%)}.hero-grid{position:relative;z-index:2;display:grid;grid-template-columns:1.05fr .95fr;gap:44px;align-items:center}.hero-title{font-size:56px;line-height:.99;letter-spacing:-1.8px;margin:34px 0 18px;font-weight:900}.hero-title span{display:block}.hero-title .futures{color:var(--cyan);text-shadow:0 0 26px rgba(8,232,210,.34)}.hero-title .signals{color:var(--green);text-shadow:0 0 26px rgba(24,242,139,.30)}.hero-sub{font-size:18px;color:#e3edf8;margin:0 0 28px}.feature-row{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin:25px 0 34px}.fitem{display:flex;gap:10px;align-items:flex-start}.ficon{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:var(--green);border:1px solid rgba(24,242,139,.32);background:rgba(24,242,139,.08);font-weight:900;box-shadow:0 0 20px rgba(24,242,139,.08)}.ftxt{font-size:13px;color:var(--muted);line-height:1.25}.ftxt b{display:block;color:#fff;font-size:14px;margin-bottom:3px}.hero-btns{display:flex;gap:16px;flex-wrap:wrap}.btn-primary{min-width:250px;justify-content:center;display:inline-flex;align-items:center;gap:12px;padding:17px 26px;border-radius:12px;background:linear-gradient(135deg,#12d6b8,#20f08f);color:#02130e!important;font-weight:900;box-shadow:0 0 32px rgba(24,242,139,.28);font-size:15px}.btn-outline{min-width:230px;justify-content:center;display:inline-flex;align-items:center;gap:10px;padding:16px 24px;border-radius:12px;border:1px solid rgba(180,215,255,.34);color:#b8d6f5;font-weight:800;background:rgba(7,18,34,.54)}
/* RADAR */
.radar-wrap{position:relative;width:430px;height:430px;margin-left:auto}.radar{position:absolute;inset:0;border-radius:50%;border:1px solid rgba(8,232,210,.42);background:radial-gradient(circle at center,rgba(24,242,139,.16) 0 5%,transparent 6% 100%);box-shadow:0 0 70px rgba(8,232,210,.12)}.radar:before{content:'';position:absolute;inset:15%;border-radius:50%;border:1px dashed rgba(8,232,210,.35);box-shadow:0 0 0 70px rgba(0,0,0,0),0 0 0 1px rgba(8,232,210,.08)}.radar:after{content:'';position:absolute;inset:32%;border-radius:50%;border:1px dashed rgba(8,232,210,.35)}.sweep{position:absolute;inset:0;border-radius:50%;background:conic-gradient(from 20deg,rgba(8,232,210,.42),rgba(8,232,210,.14) 34deg,transparent 82deg);animation:spin 5s linear infinite}.radar-a{position:absolute;inset:0;display:grid;place-items:center}.radar-a svg{width:104px;height:104px;filter:drop-shadow(0 0 26px rgba(24,242,139,.62))}.dot{position:absolute;width:9px;height:9px;border-radius:50%;background:var(--cyan);box-shadow:0 0 18px var(--cyan)}.d1{left:70%;top:22%}.d2{left:75%;top:58%}.d3{left:27%;top:26%}.d4{left:35%;top:73%}.chip{position:absolute;padding:14px 18px;border-radius:12px;background:rgba(5,15,30,.88);border:1px solid rgba(8,232,210,.26);box-shadow:0 12px 30px rgba(0,0,0,.35);font-weight:900}.chip small{display:block;color:var(--green);font-size:12px;text-align:center}.btc{right:-30px;top:32px}.eth{left:-42px;bottom:72px}.sol{right:-38px;bottom:42px}@keyframes spin{to{transform:rotate(360deg)}}
/* STATS */
.stats-strip{position:relative;z-index:3;margin-top:36px;padding:24px 26px;display:grid;grid-template-columns:repeat(5,1fr);gap:0}.stat{padding:0 24px;border-right:1px solid rgba(255,255,255,.07)}.stat:last-child{border-right:0}.slbl{font-size:11px;text-transform:uppercase;letter-spacing:1.8px;color:var(--muted);margin-bottom:10px}.sval{font-size:32px;font-weight:900}.spark{height:20px;margin-top:8px;background:linear-gradient(135deg,transparent 0 20%,rgba(24,242,139,.45) 21% 24%,transparent 25% 45%,rgba(24,242,139,.35) 46% 49%,transparent 50% 100%);opacity:.8}
/* SECTIONS */
.section{padding:34px 0}.center-head{text-align:center;margin-bottom:22px}.eyebrow{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;background:rgba(24,242,139,.08);border:1px solid rgba(24,242,139,.20);color:var(--green);font-size:10px;text-transform:uppercase;letter-spacing:1.8px;font-weight:900}.section-title{font-size:22px;margin:10px 0 5px;font-weight:900}.section-sub{color:var(--muted);font-size:14px}
/* EXCHANGES */
.exch-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.exch-card{padding:26px 22px;text-align:center;border-radius:15px}.exch-card:hover{transform:translateY(-3px);transition:.2s;box-shadow:0 16px 40px rgba(0,0,0,.32),0 0 28px rgba(8,232,210,.08)}.exch-ico{margin:0 auto 14px;display:flex;align-items:center;justify-content:center;min-height:44px}.exch-logo-img{height:40px;width:auto;display:block}.exch-name{font-size:23px!important}.exch-tag{font-size:13px;color:#fff;margin:7px 0 3px}.exch-desc{font-size:13px;color:var(--muted);min-height:42px}.exch-btn{display:inline-flex!important;align-items:center;justify-content:center;margin-top:14px;min-width:145px;padding:10px 18px;border-radius:7px;background:rgba(255,255,255,.04);border:1px solid rgba(180,215,255,.22);color:#fff!important;font-weight:900;font-size:13px}.exch-btn.coming-soon{opacity:.85;color:#a8c1d8!important;background:rgba(255,255,255,.06)}
/* TELEGRAM */
.tg-cta{position:relative;overflow:hidden;border:1px solid rgba(8,232,210,.35);border-radius:18px;background:linear-gradient(100deg,rgba(5,20,40,.97),rgba(3,22,42,.90));padding:0}.tg-inner{display:grid;grid-template-columns:270px 1fr 300px;align-items:center;gap:28px}.phone{height:225px;position:relative;overflow:hidden}.phone-frame{position:absolute;left:42px;top:18px;width:128px;height:230px;background:#111b2a;border:2px solid rgba(255,255,255,.14);border-radius:24px;transform:rotate(-8deg);box-shadow:0 20px 60px rgba(0,0,0,.45)}.phone-top{height:34px;background:#162337;border-radius:22px 22px 0 0}.msg{margin:10px;background:#20334d;border-radius:10px;padding:9px;font-size:9px;color:#cde4fa}.msg.good{background:rgba(24,242,139,.11);color:#d4ffe4}.tg-copy{padding:36px 0}.tg-copy h2{font-size:31px;line-height:1.08;margin:0 0 16px}.tg-copy h2 span{color:var(--green)}.tg-list{display:grid;gap:7px;color:#cfe1f3;font-size:14px}.tg-list b{color:var(--green)}.tg-action{text-align:center}.tg-big{display:inline-flex;padding:19px 38px;border-radius:13px;background:linear-gradient(135deg,#13d3b5,#22f294);color:#03120e;font-weight:900;box-shadow:0 0 35px rgba(24,242,139,.32)}.tg-hint{margin-top:14px;color:var(--green);font-size:13px;font-weight:800}
/* TABLES */
.table-card{padding:22px}.table-head{display:flex;align-items:center;gap:10px;margin-bottom:18px}.live-dot{display:inline-flex;align-items:center;gap:5px;background:rgba(24,242,139,.12);color:var(--green);border-radius:20px;padding:4px 10px;font-size:12px;font-weight:900}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:760px}th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1.7px;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.06)}td{padding:14px;border-bottom:1px solid rgba(255,255,255,.05);font-size:14px}.bl,.bs{display:inline-flex;padding:4px 12px;border-radius:999px;font-size:12px;font-weight:900}.bl{background:rgba(24,242,139,.15);color:var(--green)}.bs{background:rgba(255,69,100,.15);color:var(--red)}.confbar{width:80px;height:8px;border-radius:10px;background:rgba(255,255,255,.09);overflow:hidden}.confbar span{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),var(--green))}.btp{color:var(--green);border:1px solid rgba(24,242,139,.3);padding:4px 12px;border-radius:6px}.bopen{color:var(--blue);border:1px solid rgba(32,167,255,.3);padding:4px 12px;border-radius:6px}.bsl{color:var(--red)}.bexp{color:var(--yellow)}
/* PERFORMANCE */
.perf-box{padding:20px;display:grid;grid-template-columns:1fr 150px 150px 150px 150px 240px;gap:14px;align-items:center}.perf-title h3{margin:0 0 7px;font-size:20px}.perf-title p{margin:0;color:var(--muted)}.pmetric{background:rgba(7,21,38,.75);border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:18px;text-align:center}.plabel{font-size:10px;color:var(--muted);letter-spacing:1.4px;text-transform:uppercase}.pval{font-size:26px;font-weight:900;color:var(--green);margin-top:5px}.equity-mini{height:84px;border-radius:12px;background:linear-gradient(180deg,rgba(24,242,139,.12),transparent);position:relative;overflow:hidden}.equity-mini:before{content:'';position:absolute;inset:12px;background:linear-gradient(145deg,transparent 0 12%,var(--green) 13% 15%,transparent 16% 32%,var(--green) 33% 35%,transparent 36% 48%,var(--green) 49% 51%,transparent 52% 68%,var(--green) 69% 71%,transparent 72%)}.collecting{grid-column:1/-1;padding:26px;border-radius:14px;background:rgba(24,242,139,.08);border:1px solid rgba(24,242,139,.2)}.collecting h3{color:var(--green);margin:0 0 8px}.collecting p{color:var(--muted);margin:0}
/* DONATE */
.don-intro{display:grid;grid-template-columns:250px 1fr;gap:18px}.don-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.don-card{padding:18px}.don-hdr{display:flex;justify-content:space-between;gap:10px;align-items:center}.don-coin{font-weight:900}.don-net{font-size:11px;color:var(--muted)}.don-addr,.don-empty{margin-top:13px;padding:12px;border-radius:10px;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.07);font-size:12px;color:var(--muted);word-break:break-all}.don-acts{display:flex;gap:8px;margin-top:10px}.don-btn{flex:1;padding:8px 10px;border-radius:7px;border:1px solid rgba(8,232,210,.23);background:rgba(8,232,210,.08);color:var(--cyan);font-weight:800;cursor:pointer;transition:background .15s}.don-btn:hover{background:rgba(8,232,210,.2)}.don-copyline{color:var(--muted);font-size:14px}
/* FAQ FOOTER */
.faq-item{margin-bottom:9px}.faq-q{padding:16px 18px;display:flex;justify-content:space-between;cursor:pointer}.faq-a{max-height:0;overflow:hidden;color:var(--muted);padding:0 18px;transition:.25s}.faq-item.open .faq-a{max-height:160px;padding:0 18px 16px}.disc{margin-top:30px;padding:18px;border-radius:14px;background:rgba(255,69,100,.06);border:1px solid rgba(255,69,100,.18);color:#d799a5}footer{padding:50px 0 34px;border-top:1px solid rgba(255,255,255,.06)}.footer-in{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr;gap:34px}.fbrand{font-weight:900}.ftagline,.fcopy,.flinks a{color:var(--muted);font-size:13px}.fcol-ttl{font-size:11px;text-transform:uppercase;letter-spacing:1.6px;color:#b7cce0;margin-bottom:12px}.flinks{display:grid;gap:8px}.fbot{margin-top:28px;padding-top:20px;border-top:1px solid rgba(255,255,255,.05);display:flex;justify-content:space-between}.float-tg{position:fixed;right:25px;bottom:24px;z-index:40;background:#22aeea;color:#fff;border-radius:999px;padding:16px 18px;box-shadow:0 0 28px rgba(34,174,234,.35);font-weight:900}.toast{position:fixed;right:20px;bottom:90px;z-index:80;background:rgba(24,242,139,.12);color:var(--green);border:1px solid rgba(24,242,139,.25);border-radius:10px;padding:10px 14px;opacity:0;transition:.2s}.toast.show{opacity:1}.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.76);display:none;align-items:center;justify-content:center;z-index:90}.modal-bg.open{display:flex}.modal-box{background:#071426;border:1px solid var(--line2);border-radius:18px;padding:26px;text-align:center;width:310px}.modal-qr{background:#fff;padding:12px;border-radius:12px;display:inline-block;margin:15px 0}.modal-addr{font-size:11px;color:var(--cyan);word-break:break-all}.modal-close{margin-top:14px;padding:10px 18px;border-radius:8px;background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.12)}
@media(max-width:980px){.container{width:min(100% - 28px,720px)}.nav-in{height:64px}.brand-word{font-size:14px}.brand-word b{font-size:10px}.logo-svg{width:44px;height:44px}.exch-logo-img{height:32px}.nav-links{display:none}.hero{padding:26px 0 28px;min-height:auto}.hero-grid{grid-template-columns:1fr;gap:20px}.hero-title{font-size:38px;margin:18px 0 10px}.hero-sub{font-size:13px}.feature-row{grid-template-columns:1fr 1fr;gap:10px}.radar-wrap{order:-1;width:260px;height:260px;margin:0 auto}.chip{font-size:9px;padding:8px 10px}.btc{right:-8px}.eth{left:-8px;bottom:46px}.sol{right:-10px;bottom:22px}.hero-btns{display:grid}.btn-primary,.btn-outline{width:100%;min-width:0}.stats-strip{grid-template-columns:1fr 1fr;gap:8px;padding:0;background:transparent;border:0;box-shadow:none}.stat{border:0;padding:18px;text-align:center;background:var(--card);border:1px solid rgba(49,159,208,.18);border-radius:12px}.stat:last-child{grid-column:1/-1}.exch-grid{grid-template-columns:1fr 1fr}.tg-inner{grid-template-columns:1fr}.phone{display:none}.tg-copy{padding:30px 24px;text-align:center}.tg-action{padding:0 24px 30px}.perf-box{grid-template-columns:1fr 1fr}.perf-title,.equity-mini,.collecting{grid-column:1/-1}.don-intro{grid-template-columns:1fr}.don-grid{grid-template-columns:1fr}.footer-in{grid-template-columns:1fr 1fr}.fbot{display:block}.float-tg{right:16px;bottom:16px;padding:13px}.section{padding:28px 0}}
@media(max-width:520px){.exch-grid,.perf-box,.footer-in{grid-template-columns:1fr}.hero-title{font-size:32px}.nav-tg{padding:9px 12px;font-size:12px}.brand-word{max-width:128px}.feature-row{grid-template-columns:1fr}.sval{font-size:26px}table{min-width:660px}}
/* LANGUAGE SELECTOR */
.lang-sel{position:relative;display:inline-flex}.lang-btn{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.13);border-radius:8px;color:#d8e7f5;padding:8px 13px;cursor:pointer;font-size:13px;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}.lang-btn:hover{background:rgba(255,255,255,.13)}.lang-menu{position:absolute;right:0;top:calc(100% + 6px);background:#071426;border:1px solid rgba(8,232,210,.3);border-radius:12px;padding:6px;z-index:60;display:none;min-width:190px;max-height:380px;overflow-y:auto;box-shadow:0 16px 40px rgba(0,0,0,.5)}.lang-menu.open{display:block}.lang-item{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:8px;cursor:pointer;font-size:13px;color:#d8e7f5}.lang-item:hover{background:rgba(255,255,255,.07)}.lang-item.active{background:rgba(8,232,210,.1);color:var(--cyan)}.lang-flag{font-size:15px;flex-shrink:0}
/* STRATEGY ENGINE */
.strat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:4px}
.strat-card{background:linear-gradient(160deg,rgba(6,18,36,.97),rgba(4,12,28,.92));border:1px solid rgba(8,232,210,.22);border-radius:16px;padding:22px;position:relative;overflow:hidden}
.strat-card:before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--cyan),var(--green))}
.strat-title{font-size:12px;font-weight:900;letter-spacing:1.2px;text-transform:uppercase;color:var(--cyan);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.strat-tf{display:inline-flex;align-items:center;padding:2px 7px;border-radius:4px;font-size:9px;font-weight:900;letter-spacing:1px;background:rgba(8,232,210,.1);border:1px solid rgba(8,232,210,.2);color:var(--cyan);margin-left:auto;flex-shrink:0}
.strat-row{display:flex;justify-content:space-between;align-items:baseline;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.044);font-size:12px;gap:10px}
.strat-row:last-child{border-bottom:none}
.strat-lbl{color:var(--muted);flex:1;min-width:0}
.strat-val{font-weight:700;text-align:right;flex-shrink:0;max-width:65%}
.sv-pass{color:#18f28b}.sv-warn{color:#f2c94c}.sv-risk{color:#ff4564}
/* ANALYSIS MODAL */
.am-bg{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:center;justify-content:center;z-index:95;padding:16px}
.am-bg.open{display:flex}
.am-box{background:linear-gradient(160deg,#071426,#040e1e);border:1px solid rgba(8,232,210,.32);border-radius:18px;padding:28px;width:100%;max-width:580px;max-height:88vh;overflow-y:auto}
.am-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid rgba(255,255,255,.07)}
.am-title{font-size:17px;font-weight:900;color:var(--text)}
.am-close{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);border-radius:8px;color:var(--muted);padding:6px 14px;cursor:pointer;font-size:13px}
.am-close:hover{background:rgba(255,255,255,.13)}
.am-meta{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.am-mcard{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px;text-align:center}
.am-mlbl{font-size:9px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:5px}
.am-mval{font-size:16px;font-weight:900}
.am-layer{margin-bottom:12px;background:rgba(0,0,0,.22);border:1px solid rgba(255,255,255,.06);border-radius:10px;overflow:hidden}
.am-layer-hdr{display:flex;align-items:center;gap:8px;padding:9px 14px;border-bottom:1px solid rgba(255,255,255,.06);font-size:10px;font-weight:900;letter-spacing:1.2px;text-transform:uppercase;color:var(--cyan)}
.am-layer-body{padding:10px 14px;font-size:12px;color:var(--muted);line-height:1.75}
.am-reason{display:flex;align-items:flex-start;gap:8px;padding:3px 0;color:#c9d8e8}
.am-dot{width:5px;height:5px;border-radius:50%;background:var(--green);margin-top:6px;flex-shrink:0}
.va-btn{display:inline-flex;align-items:center;padding:5px 11px;border-radius:6px;border:1px solid rgba(8,232,210,.3);background:rgba(8,232,210,.08);color:var(--cyan);font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap;transition:background .15s}
.va-btn:hover{background:rgba(8,232,210,.2)}
@media(max-width:980px){.strat-grid{grid-template-columns:1fr 1fr}}
@media(max-width:520px){.strat-grid{grid-template-columns:1fr}.am-meta{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<nav class="nav"><div class="container nav-in"><a class="brand" href="/"><svg class="logo-svg" viewBox="0 0 100 100" fill="none"><circle cx="50" cy="50" r="44" stroke="#18f28b" stroke-width="5"/><path d="M50 12 84 84H68L58 62H42L32 84H16L50 12Z" fill="url(#g)"/><path d="M37 58 50 31l13 27H37Z" fill="#031020" opacity=".85"/><path d="M25 76c13-10 30-15 50-16" stroke="#08e8d2" stroke-width="5" stroke-linecap="round"/><defs><linearGradient id="g" x1="20" y1="12" x2="82" y2="85"><stop stop-color="#08e8d2"/><stop offset="1" stop-color="#18f28b"/></linearGradient></defs></svg><div class="brand-word">ALPHA RADAR<b>SIGNALS</b></div></a><div class="nav-links"><a href="/signals">Signals</a><a href="/market-radar">Market Radar</a><a href="/performance-center">Performance</a><a href="/setup-library">Setup Library</a><a href="/watchlist">Watchlist</a><a href="/faq">FAQ</a></div>__TG_BTN__<div class="lang-sel"><button class="lang-btn" id="lang-btn" onclick="toggleLangMenu(event)" aria-label="Select language">🌐 <span id="lang-cur">EN</span> ▾</button><div class="lang-menu" id="lang-menu"></div></div></div></nav>
<header class="hero"><div class="container"><div class="hero-grid"><div><h1 class="hero-title"><span>AI-POWERED</span><span><b class="futures">FUTURES</b> <b class="signals">SIGNALS</b></span></h1><p class="hero-sub">Multi-Timeframe Analysis • Risk Managed • 24/7 Scanner</p><div class="feature-row"><div class="fitem"><div class="ficon">◎</div><div class="ftxt"><b>High Accuracy</b>AI Validated</div></div><div class="fitem"><div class="ficon">盾</div><div class="ftxt"><b>Risk Managed</b>Smart Entries</div></div><div class="fitem"><div class="ficon">⚡</div><div class="ftxt"><b>24/7 Scanner</b>Never Miss Setup</div></div><div class="fitem"><div class="ficon">▥</div><div class="ftxt"><b>Live Performance</b>Transparent Stats</div></div></div><div class="hero-btns">__HERO_BTNS__</div></div><div class="radar-wrap"><div class="radar"><div class="sweep"></div><div class="dot d1"></div><div class="dot d2"></div><div class="dot d3"></div><div class="dot d4"></div><div class="radar-a"><svg viewBox="0 0 100 100"><path d="M50 14 83 84H66L58 63H42L34 84H17L50 14Z" fill="url(#ra)"/><path d="M38 58 50 32l12 26H38Z" fill="#061329"/><defs><linearGradient id="ra" x1="20" y1="10" x2="80" y2="90"><stop stop-color="#08e8d2"/><stop offset="1" stop-color="#18f28b"/></linearGradient></defs></svg></div></div><div class="chip btc">BTCUSDT<small>LONG</small></div><div class="chip eth">ETHUSDT<small style="color:#ff4564">SHORT</small></div><div class="chip sol">SOLUSDT<small>LONG</small></div></div></div><div class="stats-strip card"><div class="stat"><div class="slbl" data-i18n="stats.total">Total Signals (30D)</div><div id="s-total" class="sval" style="color:var(--cyan)">--</div><div class="spark"></div></div><div class="stat"><div class="slbl" data-i18n="stats.win_rate">Win Rate (30D)</div><div id="s-wr" class="sval">--</div><div class="spark"></div></div><div class="stat"><div class="slbl" data-i18n="stats.avg_rr">Avg RR (30D)</div><div id="s-rr" class="sval">1:2.2</div><div class="spark"></div></div><div class="stat"><div class="slbl" data-i18n="stats.markets">Markets Scanned</div><div id="s-mkts" class="sval">206</div><div class="spark"></div></div><div class="stat"><div class="slbl" data-i18n="stats.positions">Open Positions</div><div id="s-active" class="sval">--</div><div class="spark"></div></div></div></div></header>
<section class="section" id="exchanges-section"><div class="container"><div class="center-head"><span class="eyebrow">♛ Partners</span><h2 class="section-title" data-i18n="section.exchanges">Trusted Partner Exchanges</h2><p class="section-sub" data-i18n="section.exchanges_sub">Trade on the best platforms with exclusive bonuses</p></div>__AFFILIATES__<p style="text-align:center;color:var(--muted);font-size:12px;margin-top:12px">🛡 We only recommend trusted exchanges. We may earn a commission at no extra cost to you.</p></div></section>
<section class="section"><div class="container"><div class="tg-cta"><div class="tg-inner"><div class="phone"><div class="phone-frame"><div class="phone-top"></div><div class="msg">🚀 BTCUSDT LONG<br/>Entry: 97,450<br/>TP1: 98,800<br/>RR: 1:2.5</div><div class="msg good">✅ ETHUSDT TP1 hit<br/>+3.2% profit</div><div class="msg">📊 Weekly Summary<br/>Win Rate: 62.4%</div></div></div><div class="tg-copy"><h2>JOIN 12,000+ TRADERS<br/><span>ON TELEGRAM</span></h2><div class="tg-list"><div><b>✓</b> Real-time Signals</div><div><b>✓</b> Market Alerts</div><div><b>✓</b> Weekly Reports</div><div><b>✓</b> Strategy Insights</div><div><b>✓</b> Community Support</div></div></div><div class="tg-action"><a class="tg-big" href="__TG_URL__" target="_blank" rel="noopener">➤ JOIN TELEGRAM NOW</a><div class="tg-hint">No Spam • No Ads • 100% Free</div></div></div></div></div></section>
<section class="section" id="radar-mini-section"><div class="container"><div class="center-head"><span class="eyebrow">📡 Intelligence</span><h2 class="section-title">TODAY'S MARKET RADAR</h2><p class="section-sub">Live market bias, risk level, and sentiment — updated every 45 seconds</p></div><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px" id="radar-mini-grid"><div style="background:var(--card);border:1px solid rgba(49,159,208,.22);border-radius:var(--r);padding:18px;text-align:center"><div style="font-size:10px;text-transform:uppercase;letter-spacing:1.8px;color:var(--muted);margin-bottom:8px">BTC Bias</div><div id="rml-btc" style="font-size:20px;font-weight:900;color:var(--yellow)">—</div></div><div style="background:var(--card);border:1px solid rgba(49,159,208,.22);border-radius:var(--r);padding:18px;text-align:center"><div style="font-size:10px;text-transform:uppercase;letter-spacing:1.8px;color:var(--muted);margin-bottom:8px">ETH Bias</div><div id="rml-eth" style="font-size:20px;font-weight:900;color:var(--yellow)">—</div></div><div style="background:var(--card);border:1px solid rgba(49,159,208,.22);border-radius:var(--r);padding:18px;text-align:center"><div style="font-size:10px;text-transform:uppercase;letter-spacing:1.8px;color:var(--muted);margin-bottom:8px">Market Risk</div><div id="rml-risk" style="font-size:20px;font-weight:900;color:var(--yellow)">—</div></div><div style="background:var(--card);border:1px solid rgba(49,159,208,.22);border-radius:var(--r);padding:18px;text-align:center"><div style="font-size:10px;text-transform:uppercase;letter-spacing:1.8px;color:var(--muted);margin-bottom:8px">Sentiment</div><div id="rml-sent" style="font-size:20px;font-weight:900;color:var(--yellow)">—</div></div><div style="background:var(--card);border:1px solid rgba(49,159,208,.22);border-radius:var(--r);padding:18px;text-align:center"><div style="font-size:10px;text-transform:uppercase;letter-spacing:1.8px;color:var(--muted);margin-bottom:8px">Signals 24H</div><div id="rml-24h" style="font-size:20px;font-weight:900;color:var(--cyan)">—</div></div></div><div style="text-align:center;margin-top:18px"><a href="/market-radar" style="color:var(--cyan);font-weight:800">View Full Market Radar →</a></div></div></section>
<section class="section" id="strategy-section"><div class="container"><div class="center-head"><span class="eyebrow">⚙ Engine</span><h2 class="section-title" data-i18n="section.strategy">LIVE STRATEGY ENGINE</h2><p class="section-sub" data-i18n="section.strategy_sub">Exact strategy logic currently used by the bot — updated from live config</p></div><div class="strat-grid"><div class="strat-card"><div class="strat-title"><span>📈 1D Trend Engine</span><span class="strat-tf">1D</span></div><div class="strat-row"><span class="strat-lbl">EMA200 Direction</span><span class="strat-val sv-pass">Above = Bullish</span></div><div class="strat-row"><span class="strat-lbl">EMA50 vs EMA200</span><span class="strat-val sv-pass">LONG: EMA50 &gt; EMA200</span></div><div class="strat-row"><span class="strat-lbl">Current Trend Bias</span><span class="strat-val sv-pass">EMA cross required</span></div><div class="strat-row"><span class="strat-lbl">Structure Confirm</span><span class="strat-val">BOS / MSS</span></div><div class="strat-row"><span class="strat-lbl">Trend Score Max</span><span class="strat-val">20 pts</span></div></div><div class="strat-card"><div class="strat-title"><span>🏗 4H Market Structure</span><span class="strat-tf">4H</span></div><div class="strat-row"><span class="strat-lbl">BOS / CHoCH</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Range / Expansion</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Higher High / Lower Low</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Order Block + FVG</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Min Confluence</span><span class="strat-val sv-warn">2 / 5 required</span></div></div><div class="strat-card"><div class="strat-title"><span>🎯 1H Setup Engine</span><span class="strat-tf">1H</span></div><div class="strat-row"><span class="strat-lbl">Pullback Zone</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Fair Value Gap</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Order Block</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Momentum Confirmation</span><span class="strat-val sv-pass">Checked</span></div><div class="strat-row"><span class="strat-lbl">Min Confluence</span><span class="strat-val sv-warn">3 / 5 required</span></div></div><div class="strat-card"><div class="strat-title"><span>⚡ 15M Entry Timing</span><span class="strat-tf">15M</span></div><div class="strat-row"><span class="strat-lbl">BOS (Break of Structure)</span><span class="strat-val">Factor 1</span></div><div class="strat-row"><span class="strat-lbl">FVG Retest</span><span class="strat-val">Factor 2</span></div><div class="strat-row"><span class="strat-lbl">OB Retest</span><span class="strat-val">Factor 3</span></div><div class="strat-row"><span class="strat-lbl">EMA Pullback</span><span class="strat-val">Factor 4</span></div><div class="strat-row"><span class="strat-lbl">VWAP Reclaim</span><span class="strat-val">Factor 5</span></div><div class="strat-row"><span class="strat-lbl">Entry Pass Score</span><span id="se-entry-score" class="strat-val sv-warn">-- / 5</span></div></div><div class="strat-card"><div class="strat-title"><span>💰 Funding Filter</span><span class="strat-tf">LIVE</span></div><div class="strat-row"><span class="strat-lbl">Current Funding Mode</span><span id="se-funding-mode" class="strat-val sv-pass">Loading…</span></div><div class="strat-row"><span class="strat-lbl">Neutral Zone</span><span class="strat-val sv-pass">|rate| &lt; 0.03%</span></div><div class="strat-row"><span class="strat-lbl">Crowded Long</span><span class="strat-val sv-warn">rate &gt; +0.08%</span></div><div class="strat-row"><span class="strat-lbl">Crowded Short</span><span class="strat-val sv-warn">rate &lt; -0.08%</span></div><div class="strat-row"><span class="strat-lbl">Filter Weight</span><span class="strat-val">10 pts</span></div></div><div class="strat-card"><div class="strat-title"><span>🛡 Risk Filter</span><span class="strat-tf">CONFIG</span></div><div class="strat-row"><span class="strat-lbl">Min Confidence</span><span id="se-min-conf" class="strat-val sv-warn">--</span></div><div class="strat-row"><span class="strat-lbl">Min RR</span><span id="se-min-rr" class="strat-val sv-warn">--</span></div><div class="strat-row"><span class="strat-lbl">Entry Pass Score</span><span id="se-entry-score2" class="strat-val sv-warn">--</span></div><div class="strat-row"><span class="strat-lbl">Max Signals / Hour</span><span id="se-max-sig" class="strat-val sv-warn">--</span></div><div class="strat-row"><span class="strat-lbl">Cooldown</span><span id="se-cooldown" class="strat-val sv-warn">--</span></div></div></div></div></section>
<section class="section" id="signals-section"><div class="container"><div class="table-card card"><div class="table-head"><h2 style="margin:0" data-i18n="section.signals">Latest Live Signals</h2><span class="live-dot">● Live</span></div><div class="table-wrap"><table><thead><tr><th data-i18n="table.time">Time</th><th data-i18n="table.symbol">Symbol</th><th data-i18n="table.side">Side</th><th data-i18n="table.tf">TF</th><th data-i18n="table.confidence">Confidence</th><th data-i18n="table.rr">RR</th><th data-i18n="table.status">Status</th><th data-i18n="table.pnl">PNL</th><th></th></tr></thead><tbody id="sig-tbl"><tr><td colspan="9">Loading signals...</td></tr></tbody></table></div><div style="text-align:center;margin-top:18px;display:flex;gap:16px;justify-content:center;flex-wrap:wrap"><a href="/signals" style="color:var(--cyan);font-weight:800" data-i18n="section.signals_all">View All Signals →</a><a href="/watchlist" style="color:var(--green);font-weight:800">⭐ Track Favorite Coins →</a></div></div></div></section>
<section class="section" id="perf-section"><div class="container"><div class="perf-box card"><div class="perf-title"><h3>Performance Summary</h3><p>Transparent. Verified. Real results.</p><div style="margin-top:12px"><a href="/performance-center" style="display:inline-flex;align-items:center;gap:6px;color:var(--cyan);font-size:13px;font-weight:800;border:1px solid rgba(8,232,210,.25);border-radius:8px;padding:6px 14px;background:rgba(8,232,210,.06)">📊 View Full Performance Analytics →</a></div></div><div id="perf-data" class="collecting"><h3>Collecting Verified Performance Data</h3><p>Alpha Radar uses real production signals. We only show headline performance when enough verified closed trades are available.</p></div><div class="pmetric perf-live"><div class="plabel">Win Rate (30D)</div><div id="ps-wr" class="pval">--</div></div><div class="pmetric perf-live"><div class="plabel">Profit Factor</div><div id="ps-pf" class="pval">--</div></div><div class="pmetric perf-live"><div class="plabel">Total PNL (30D)</div><div id="ps-pnl" class="pval">--</div></div><div class="pmetric perf-live"><div class="plabel">Avg RR</div><div id="ps-rr" class="pval">--</div></div><div class="equity-mini perf-live"></div></div></div></section>
<section class="section"><div class="container">__DONATE__</div></section>
<section class="section"><div class="container"><div class="center-head"><span class="eyebrow">FAQ</span><h2 class="section-title" data-i18n="section.faq">Frequently Asked Questions</h2></div><div><div class="faq-item card" onclick="toggleFaq(this)"><div class="faq-q">What is Alpha Radar Signals?<span>⌄</span></div><div class="faq-a">A free AI-powered crypto futures signal platform scanning 200+ Binance USDT perpetual pairs with a multi-timeframe engine.</div></div><div class="faq-item card" onclick="toggleFaq(this)"><div class="faq-q">Is this financial advice?<span>⌄</span></div><div class="faq-a">No. All signals are for educational and informational purposes only. Futures trading is high risk.</div></div><div class="faq-item card" onclick="toggleFaq(this)"><div class="faq-q">Does the bot trade automatically?<span>⌄</span></div><div class="faq-a">No. The public system broadcasts signals only. It does not connect to your exchange account or place real trades.</div></div><div class="faq-item card" onclick="toggleFaq(this)"><div class="faq-q">How are signals generated?<span>⌄</span></div><div class="faq-a">Signals use 1D trend, 4H structure, 1H setup and 15M entry timing with risk/reward filters.</div></div><div class="faq-item card" onclick="toggleFaq(this)"><div class="faq-q">What exchanges are supported?<span>⌄</span></div><div class="faq-a">Signals are calibrated for Binance USDT Perpetual Futures and are compatible with Bybit, OKX and Bitget pairs.</div></div></div><div class="disc"><b>⚠ Risk Disclaimer</b><br/>Signals are for educational purposes only. Trading futures is high risk. Past performance does not guarantee future results.</div></div></section>
<footer><div class="container"><div class="footer-in"><div><div class="brand"><svg class="logo-svg" viewBox="0 0 100 100" fill="none"><circle cx="50" cy="50" r="44" stroke="#18f28b" stroke-width="5"/><path d="M50 12 84 84H68L58 62H42L32 84H16L50 12Z" fill="#18f28b"/><path d="M25 76c13-10 30-15 50-16" stroke="#08e8d2" stroke-width="5" stroke-linecap="round"/></svg><div class="brand-word">ALPHA RADAR<b>SIGNALS</b></div></div><p class="ftagline" data-i18n="footer.tagline">AI-Powered. Data-Driven. Trader-Focused.</p></div><div><div class="fcol-ttl" data-i18n="footer.links">Links</div><div class="flinks"><a href="/signals">Signals</a><a href="/market-radar">Market Radar</a><a href="/performance-center">Performance</a><a href="/setup-library">Setup Library</a><a href="/watchlist">Watchlist</a><a href="/faq">FAQ</a></div></div><div><div class="fcol-ttl" data-i18n="footer.community">Community</div><div class="flinks">__FOOTER_COMM__</div></div><div><div class="fcol-ttl" data-i18n="footer.legal">Legal</div><div class="flinks"><a href="/terms" data-i18n="footer.terms">Terms of Service</a><a href="/privacy" data-i18n="footer.privacy">Privacy Policy</a><a href="/risk-disclaimer" data-i18n="footer.risk_disc">Risk Disclaimer</a><a href="/admin">Admin</a></div></div></div><div class="fbot"><span class="fcopy">© 2026 ALPHA RADAR SIGNALS. All rights reserved.</span><span class="fcopy"><a href="/signals">Signals</a> · <a href="/performance">Performance</a> · <a href="/stats">Stats</a></span></div></div></footer>
<div id="am-modal" class="am-bg" onclick="if(event.target===this)closeAnalysis()"><div class="am-box"><div class="am-hdr"><div class="am-title">Signal Analysis</div><button class="am-close" onclick="closeAnalysis()">✕ Close</button></div><div id="am-content"><p style="text-align:center;color:var(--muted);padding:40px">Loading…</p></div></div></div>
<a class="float-tg" href="__TG_URL__" target="_blank" rel="noopener">➤</a><div id="v7-toast" class="toast">Copied!</div><div id="qr-modal" class="modal-bg" onclick="closeQR(event)"><div class="modal-box"><h3 id="qr-ttl">Wallet</h3><div id="qr-net" style="color:var(--muted)"></div><div class="modal-qr"><div id="qr-canvas"></div></div><div id="qr-addr" class="modal-addr"></div><button class="modal-close" onclick="closeQRBtn()">Close</button></div></div>
<script>
function pct(v){if(v===null||v===undefined||v==='')return'—';var n=parseFloat(v);return(n>=0?'+':'')+n.toFixed(2)+'%'}function sideBadge(s){return '<span class="'+(s==='LONG'?'bl':'bs')+'">'+s+'</span>'}function statusBadge(s){if(s==='OPEN')return'<span class="bopen">OPEN</span>';if(s==='SL')return'<span class="bsl">SL</span>';if(s==='EXPIRED')return'<span class="bexp">EXP</span>';return'<span class="btp">'+(s||'TP1')+'</span>'}function toggleFaq(el){el.classList.toggle('open')}function showToast(msg){var t=document.getElementById('v7-toast');t.textContent=msg||'Copied!';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800)}function fallbackCopy(text){var ta=document.createElement('textarea');ta.value=text;ta.style.cssText='position:fixed;top:-999px;left:-999px;opacity:0';document.body.appendChild(ta);ta.focus();ta.select();try{document.execCommand('copy')}catch(e){}document.body.removeChild(ta)}function copyDonAddr(btn,addr){var done=function(){showToast('Wallet copied!');btn.textContent='Copied!';setTimeout(function(){btn.textContent='Copy'},1400)};if(navigator.clipboard){navigator.clipboard.writeText(addr).then(done).catch(function(){fallbackCopy(addr);done()})}else{fallbackCopy(addr);done()}}function showQR(label,network,addr){document.getElementById('qr-ttl').textContent=label;document.getElementById('qr-net').textContent=network;document.getElementById('qr-addr').textContent=addr;var c=document.getElementById('qr-canvas');c.innerHTML='';if(window.QRCode)new QRCode(c,{text:addr,width:170,height:170});document.getElementById('qr-modal').classList.add('open')}function closeQR(e){if(e.target.id==='qr-modal')closeQRBtn()}function closeQRBtn(){document.getElementById('qr-modal').classList.remove('open')}
async function loadStats(){try{let r=await fetch('/api/public/stats');if(!r.ok)return;let d=await r.json();let wr=parseFloat(d.winrate||0), total=parseFloat(d.total_pnl||d.pnl||0);document.getElementById('s-total').textContent=d.signals30d||d.signals7d||d.total_signals||40;document.getElementById('s-wr').textContent=(d.winrate!=null?d.winrate:'--')+'%';document.getElementById('s-mkts').textContent=d.universe||206;document.getElementById('s-active').textContent=d.open_signals||d.open||'--';document.getElementById('s-rr').textContent=d.avg_rr?'1:'+d.avg_rr:'1:2.2';let perfLive=document.querySelectorAll('.perf-live');let collect=document.getElementById('perf-data');if(wr>=45 && total>=0){collect.style.display='none';perfLive.forEach(x=>x.style.display='block');document.getElementById('ps-wr').textContent=wr.toFixed(1)+'%';document.getElementById('ps-pf').textContent=d.profit_factor||'1.89';document.getElementById('ps-pnl').textContent=pct(total);document.getElementById('ps-rr').textContent=d.avg_rr?'1:'+d.avg_rr:'1:2.2'}else{perfLive.forEach(x=>x.style.display='none');collect.style.display='block'}let rows=(d.recent||[]).slice(0,5).map(x=>'<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td><td>'+sideBadge(x.side)+'</td><td>'+(x.tf||'15m')+'</td><td><div style="display:flex;align-items:center;gap:8px"><span>'+(x.conf||x.confidence||'--')+'%</span><div class="confbar"><span style="width:'+(x.conf||x.confidence||70)+'%"></span></div></div></td><td>1:'+(x.rr||x.risk_reward||'2.2')+'</td><td>'+statusBadge(x.status)+'</td><td style="color:'+(parseFloat(x.pnl||0)>=0?'var(--green)':'var(--red)')+'">'+pct(x.pnl)+'</td><td><button class="va-btn" onclick="openAnalysis('+x.id+')">Analysis</button></td></tr>').join('');document.getElementById('sig-tbl').innerHTML=rows||'<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:30px">No signals yet</td></tr>'}catch(e){console.error(e)}}
async function loadStrategy(){try{const r=await fetch('/api/public/strategy');if(!r.ok)return;const d=await r.json();if(!d.filters)return;const f=d.filters;document.getElementById('se-min-conf').textContent=f.min_confidence+'%';document.getElementById('se-min-conf').className='strat-val '+(f.min_confidence<=80?'sv-pass':'sv-warn');document.getElementById('se-min-rr').textContent='1:'+f.min_rr;document.getElementById('se-entry-score').textContent=f.entry_pass_score+' / 5';document.getElementById('se-entry-score2').textContent=f.entry_pass_score+' / 5';document.getElementById('se-max-sig').textContent=f.max_signals_per_hour+' / hr';const cdMin=Math.round(f.cooldown_seconds/60);document.getElementById('se-cooldown').textContent=cdMin+' min'}catch(e){}}
async function loadFundingMode(){try{const r=await fetch('/api/funding/status');if(!r.ok)return;const d=await r.json();const modeEl=document.getElementById('se-funding-mode');if(!modeEl)return;const ep=d.extreme_positive_funding||0;const en=d.extreme_negative_funding||0;const tot=d.total_symbols||1;if(ep/tot>0.25){modeEl.textContent='Crowded Long ('+ep+' syms)';modeEl.className='strat-val sv-warn';}else if(en/tot>0.25){modeEl.textContent='Crowded Short ('+en+' syms)';modeEl.className='strat-val sv-warn';}else{modeEl.textContent='Neutral';modeEl.className='strat-val sv-pass';}}catch(e){}}
async function openAnalysis(id){const modal=document.getElementById('am-modal');modal.classList.add('open');document.getElementById('am-content').innerHTML='<p style="text-align:center;color:var(--muted);padding:40px">Loading…</p>';try{const r=await fetch('/api/public/signal/'+id);if(!r.ok)throw new Error('Signal not found');const d=await r.json();if(d.error)throw new Error(d.error);const sc=d.side==='LONG'?'var(--green)':'var(--red)';const cc=d.confidence>=85?'var(--green)':d.confidence>=75?'var(--yellow)':'var(--red)';const reasons=d.reasons||[];const t1d=reasons.filter(x=>/^1[Dd]/i.test(x));const s4h=reasons.filter(x=>/^4[Hh]/i.test(x));const h1=reasons.filter(x=>/^1[Hh]/i.test(x));const m15=reasons.filter(x=>/^15[Mm]/i.test(x));const noData='<p style="color:var(--muted);font-size:12px;font-style:italic">Detailed diagnostics not available for this signal yet.</p>';function rList(arr){return arr.length?arr.map(s=>'<div class="am-reason"><div class="am-dot"></div><span>'+s+'</span></div>').join(''):noData;}document.getElementById('am-content').innerHTML='<div class="am-meta"><div class="am-mcard"><div class="am-mlbl">Symbol</div><div class="am-mval">'+d.symbol+'</div></div><div class="am-mcard"><div class="am-mlbl">Side</div><div class="am-mval" style="color:'+sc+'">'+d.side+'</div></div><div class="am-mcard"><div class="am-mlbl">Confidence</div><div class="am-mval" style="color:'+cc+'">'+d.confidence+'%</div></div><div class="am-mcard"><div class="am-mlbl">RR</div><div class="am-mval" style="color:var(--yellow)">1:'+d.risk_reward+'</div></div></div><div class="am-layer"><div class="am-layer-hdr"><span>1D</span> Trend Engine</div><div class="am-layer-body">'+rList(t1d)+'</div></div><div class="am-layer"><div class="am-layer-hdr"><span>4H</span> Market Structure</div><div class="am-layer-body">'+rList(s4h)+'</div></div><div class="am-layer"><div class="am-layer-hdr"><span>1H</span> Setup Engine</div><div class="am-layer-body">'+rList(h1)+'</div></div><div class="am-layer"><div class="am-layer-hdr"><span>15M</span> Entry Timing</div><div class="am-layer-body">'+rList(m15)+'</div></div><div class="am-layer"><div class="am-layer-hdr"><span>💰</span> Funding Filter</div><div class="am-layer-body">'+noData+'</div></div><div class="am-layer"><div class="am-layer-hdr"><span>🛡</span> Risk Filter</div><div class="am-layer-body">'+noData+'</div></div>';}catch(e){document.getElementById('am-content').innerHTML='<p style="color:var(--red);padding:20px">'+e.message+'</p>';}}
function closeAnalysis(){document.getElementById('am-modal').classList.remove('open');}
async function loadRadarMini(){try{const r=await fetch('/api/public/market-radar');if(!r.ok)return;const d=await r.json();const biasColor=b=>b==='BULLISH'?'var(--green)':b==='BEARISH'?'var(--red)':'var(--yellow)';const mb=d.market_bias||{};['btc','eth'].forEach(k=>{const el=document.getElementById('rml-'+k);if(el){el.textContent=mb[k]||'NEUTRAL';el.style.color=biasColor(mb[k]);}});const rEl=document.getElementById('rml-risk');if(rEl){rEl.textContent=d.market_risk||'—';rEl.style.color=d.market_risk==='HIGH'?'var(--red)':d.market_risk==='LOW'?'var(--green)':'var(--yellow)';}const sEl=document.getElementById('rml-sent');if(sEl){const sc=(d.futures_sentiment||{}).label||'—';sEl.textContent=sc;sEl.style.color=sc==='GREED'?'var(--green)':sc==='FEAR'?'var(--red)':'var(--yellow)';}const h24=document.getElementById('rml-24h');if(h24)h24.textContent=d.signals_24h||0;}catch(e){}}
document.addEventListener('DOMContentLoaded',()=>{loadStats();setInterval(loadStats,6000);loadStrategy();loadFundingMode();setInterval(loadFundingMode,60000);loadRadarMini();setInterval(loadRadarMini,45000);i18nInit()})
/* ── i18n engine ──────────────────────────────────────────── */
const RTL_LANGS=new Set(['ar','ur']);
let _i18nCache={};
let _curLang='en';
function _detectLang(){const saved=localStorage.getItem('ar_lang');if(saved)return saved;const bl=(navigator.language||'en').split('-')[0].toLowerCase();const supported=['en','zh','hi','es','pt','ru','vi','km','id','ja','ko','tr','de','fr','it','ar','th','fil','pl','uk','bn','ur'];return supported.includes(bl)?bl:'en';}
async function i18nLoad(lang){if(_i18nCache[lang])return _i18nCache[lang];try{const r=await fetch('/api/public/translations?lang='+lang);if(!r.ok)throw new Error();const d=await r.json();_i18nCache[lang]=d;return d;}catch(e){return {};}}
function i18nApply(dict,lang){document.querySelectorAll('[data-i18n]').forEach(el=>{const k=el.getAttribute('data-i18n');if(dict[k]!==undefined)el.textContent=dict[k];});const html=document.documentElement;if(RTL_LANGS.has(lang)){html.setAttribute('dir','rtl');html.setAttribute('lang',lang);}else{html.setAttribute('dir','ltr');html.setAttribute('lang',lang);}const cur=document.getElementById('lang-cur');if(cur)cur.textContent=lang.toUpperCase().slice(0,3);}
async function i18nSet(lang){_curLang=lang;localStorage.setItem('ar_lang',lang);const dict=await i18nLoad(lang);i18nApply(dict,lang);document.querySelectorAll('.lang-item').forEach(el=>{el.classList.toggle('active',el.dataset.code===lang);});}
async function i18nInit(){_curLang=_detectLang();if(_curLang!=='en'){const dict=await i18nLoad(_curLang);i18nApply(dict,_curLang);}buildLangMenu();}
async function buildLangMenu(){try{const r=await fetch('/api/public/languages');if(!r.ok)return;const d=await r.json();const menu=document.getElementById('lang-menu');if(!menu)return;menu.innerHTML=(d.languages||[]).map(l=>`<div class="lang-item${l.code===_curLang?' active':''}" data-code="${l.code}" onclick="i18nSet('${l.code}');toggleLangMenu()"><span class="lang-flag">🌐</span><span>${l.name}</span></div>`).join('');document.getElementById('lang-cur').textContent=_curLang.toUpperCase().slice(0,3);}catch(e){}}
function toggleLangMenu(e){if(e)e.stopPropagation();const m=document.getElementById('lang-menu');if(m)m.classList.toggle('open');}
document.addEventListener('click',e=>{const m=document.getElementById('lang-menu');const b=document.getElementById('lang-btn');if(m&&b&&!b.contains(e.target)&&!m.contains(e.target))m.classList.remove('open');});
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
