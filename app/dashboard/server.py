from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

# Re-exported from htmlpages (handlers in app/dashboard/routes import these
# from here). Extracted to shrink this module; behaviour unchanged.
from app.dashboard.htmlpages import (  # noqa: F401
    _ADMIN_HTML,
    _LOGIN_HTML,
    _PLATFORM_ADMIN_HTML,
    _PUBLIC_HTML,
    _backtest_page_html,
    _esc,
    _health_page_html,
    _info_page,
    _js_single_quote,
    _market_radar_page_html,
    _page_shell,
    _paper_page_html,
    _performance_center_page_html,
    _performance_page_html,
    _safe_url,
    _safe_wallet,
    _setup_library_page_html,
    _signal_detail_page_html,
    _signals_page_html,
    _stats_page_html,
)
from app.database.models import (
    Signal,
)
from app.database.session import SessionLocal
from app.market_data import universe
from app.utils.logger import logger
from app.utils.observability import CorrelationMiddleware, request_id_ctx
from app.utils.ratelimit import RateLimitMiddleware
from app.utils.timezone import normalize_utc_iso

# ── auth ──────────────────────────────────────────────────────────


def _admin_user() -> str:
    return os.getenv("DASHBOARD_USER", "admin")


def _admin_password() -> str:
    """Admin password must be supplied via .env in public deployments."""
    return os.getenv("DASHBOARD_PASSWORD", "").strip()


def _admin_auth_configured() -> bool:
    return bool(_admin_user() and _admin_password())


def _is_logged_in(request: Request) -> bool:
    return request.cookies.get("alpha_radar_auth") == "ok"


def _login_page(error: str = "") -> HTMLResponse:
    if not _admin_auth_configured():
        error = (
            "Admin login is disabled until DASHBOARD_USER and DASHBOARD_PASSWORD are set in .env"
        )
    err = f"<div class='err'>{_esc(error)}</div>" if error else ""
    return HTMLResponse(_LOGIN_HTML.replace("__ERR__", err))


# ── app ───────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    print("dashboard starting")
    yield


app = FastAPI(title="ARGUS QUANT", lifespan=_lifespan)
_boot_time = time.time()

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Production filter: only V3 MTF signals appear on all public-facing queries.
# Legacy 5m / old-engine signals live in archive_signals after migration.
_MTF_TIMEFRAMES = ["15m", "1h", "4h", "1d"]
_MTF_STRATEGY = "MTF_SMC_STRICT"


# Content-Security-Policy. Scoped to what the UI actually loads: Chart.js
# (cdn.jsdelivr.net), the QR widget (cdnjs.cloudflare.com), and Google Fonts.
# 'unsafe-inline' is required by the server-rendered inline <script>/<style>
# blocks and inline handlers; output is escaped at the JS layer (esc()).
# Even so the policy adds real protection: object-src/base-uri/frame-ancestors/
# form-action lockdown and a same-origin default for every other resource type.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: https://*.googleusercontent.com",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response


app.add_middleware(_SecurityHeaders)
# Per-IP rate limit on the public API surface (abuse / DoS protection). Placed
# under CorrelationMiddleware so a 429 still carries the request id.
app.add_middleware(
    RateLimitMiddleware,
    limit=settings.api_rate_limit_per_min,
    window_sec=60,
    prefixes=[p.strip() for p in settings.api_rate_limit_prefixes.split(",") if p.strip()],
    enabled=settings.api_rate_limit_enabled,
)
# Added last → outermost: assigns the correlation id before any other
# middleware runs, so security-header and route logs share one request id.
app.add_middleware(CorrelationMiddleware)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a structured 500 (no internals leaked) and log the traceback with
    the request's correlation id so the failure is traceable in production."""
    rid = request_id_ctx.get()
    logger.exception(f"unhandled error rid={rid} {request.method} {request.url.path}: {exc!r}")
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "correlation_id": rid},
    )


# ── stats helper ──────────────────────────────────────────────────


async def _get_stats() -> dict:
    now = datetime.now(timezone.utc)
    prod_start_raw = os.getenv("PRODUCTION_START_UTC", "").strip()
    try:
        start7 = (
            datetime.strptime(prod_start_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if prod_start_raw
            else now - timedelta(days=7)
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
            .order_by(desc(Signal.created_at))
            .limit(500)
        )
        week = week_res.scalars().all()
        recent_res = await session.execute(
            select(Signal)
            .where(
                Signal.strategy == _MTF_STRATEGY,
                Signal.timeframe.in_(_MTF_TIMEFRAMES),
            )
            .order_by(desc(Signal.created_at))
            .limit(20)
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
        [
            {"symbol": k, "avg": round(sum(v) / len(v), 2), "count": len(v)}
            for k, v in sym_map.items()
        ],
        key=lambda x: x["avg"],
        reverse=True,
    )[:10]

    def _row(s):
        return {
            "id": s.id,
            "time": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
            "time_iso": normalize_utc_iso(s.created_at),
            "symbol": s.symbol,
            "side": s.side,
            "tf": s.timeframe,
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


# ── V13: single aggregated dashboard read (perf) ──────────────────
# Bundles the existing public reads into ONE response so the SaaS portal
# fetches the whole dashboard in a single request. Purely additive — every
# underlying endpoint (/api/public/stats, /signals, /performance,
# /market-regime, /status) is unchanged and still served for back-compat.
# Cached server-side for 30s (separate from the 45s perf-center cache).
_dash_cache: dict = {"data": None, "ts": 0.0}
_DASH_TTL = 30.0  # seconds


async def _json_body(resp):
    """Normalise a handler return (JSONResponse or plain dict/list) to a value."""
    if isinstance(resp, JSONResponse):
        try:
            import json as _json

            return _json.loads(resp.body)
        except Exception:
            return None
    return resp


def _compute_backtest(signals: list) -> dict:
    """
    Core backtest computation — pure function, no DB calls.
    Called by both GET /api/backtest and GET /api/public/backtest.
    strategy = MTF_SMC_STRICT · timeframe IN (15m/1h/4h/1d) · closed only.
    """
    import math as _m
    from collections import defaultdict as _dd

    _EMPTY = {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "max_drawdown": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "avg_rr": 0.0,
        "avg_pnl": 0.0,
        "total_pnl": 0.0,
        "rr_distribution": [],
        "equity_curve": [0.0],
        "monthly": [],
    }
    if not signals:
        return _EMPTY

    WIN_ST = ("TP1", "TP2", "TP3")
    wins = [s for s in signals if s.status in WIN_ST]
    losses = [s for s in signals if s.status == "SL"]
    pnls = [float(s.pnl_pct or 0) for s in signals]
    rrs = [float(s.risk_reward or 0) for s in signals]
    n = len(signals)

    # ── core metrics ──────────────────────────────────────────────
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    mean_pnl = sum(pnls) / max(1, n)
    sharpe = 0.0
    if n > 1:
        var = sum((p - mean_pnl) ** 2 for p in pnls) / n
        sharpe = round(mean_pnl / max(0.001, _m.sqrt(var)), 2)

    # ── equity curve (cumulative PnL %) + max drawdown ───────────
    cum = peak = max_dd = 0.0
    equity_curve = [0.0]
    for p in pnls:
        cum = round(cum + p, 2)
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
        [{"rr": k, "count": v} for k, v in rr_buckets.items()], key=lambda x: float(x["rr"])
    )

    # ── monthly breakdown ─────────────────────────────────────────
    mo_map: dict = _dd(list)
    for s in signals:
        if s.created_at:
            mo_map[s.created_at.strftime("%Y-%m")].append(s)

    monthly_rows = []
    for month, msigs in sorted(mo_map.items()):
        mw = [s for s in msigs if s.status in WIN_ST]
        ml = [s for s in msigs if s.status == "SL"]
        mp = [float(s.pnl_pct or 0) for s in msigs]
        mn = max(1, len(msigs))
        mgw = sum(p for p in mp if p > 0)
        mgl = abs(sum(p for p in mp if p < 0))
        m_pf = round(mgw / mgl, 2) if mgl > 0 else None
        monthly_rows.append(
            {
                "month": month,
                "signals": len(msigs),
                "wins": len(mw),
                "losses": len(ml),
                "win_rate": round(len(mw) / mn * 100, 1),
                "total_pnl": round(sum(mp), 2),
                "profit_factor": m_pf,
            }
        )

    return {
        "total": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_factor": profit_factor,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd, 2),  # backward-compat alias
        "sharpe_ratio": sharpe,
        "avg_rr": round(sum(rrs) / max(1, n), 2),
        "avg_pnl": round(mean_pnl, 2),
        "total_pnl": round(sum(pnls), 2),
        "rr_distribution": rr_dist,
        "equity_curve": equity_curve[-61:],  # max 61 points (60 trades + start)
        "monthly": monthly_rows,
    }


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


# ── Sprint 13: Market Radar ───────────────────────────────────────


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


# ── Sprint 19A: Market Regime ─────────────────────────────────────


# ── Sprint 19B: Short Protection Analytics ────────────────────────


# ── monitoring (no auth) ──────────────────────────────────────────


# ── admin API (requires auth) ─────────────────────────────────────


# ── auth routes ───────────────────────────────────────────────────


# ── admin dashboard ───────────────────────────────────────────────


# ── Sprint 20H — SaaS platform admin (HTML page + cookie-gated JSON) ──────
# The page is served behind the existing dashboard cookie login and reads the
# multi-user SaaS tables through app.admin.service (the same aggregation the
# JWT /api/admin/* API uses). Credentials are never exposed (last4 only).


# ── public homepage ───────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════
#  Sprint 12 — Performance Analytics Center
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
#  Sprint 13 — Market Radar
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
#  Sprint 14 — Setup Library
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
#  Sprint 15 — Watchlist
# ══════════════════════════════════════════════════════════════════


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
    # Sprint 20E — mount the safety-layer API (loss limits + kill switches).
    if settings.safety_layer_enabled:
        try:
            from app.safety import setup_safety

            setup_safety(app)
        except Exception as exc:  # noqa: BLE001
            print(f"safety layer setup skipped (non-fatal): {exc!r}")
    # Sprint 20F — mount the live-trading API (Binance). Exposing it is gated by
    # LIVE_TRADING_API_ENABLED; REAL orders still need the execution gate.
    if settings.live_trading_api_enabled:
        try:
            from app.live_trading import setup_live

            setup_live(app)
        except Exception as exc:  # noqa: BLE001
            print(f"live trading setup skipped (non-fatal): {exc!r}")
    # Sprint 20H — mount the ADMIN-only platform oversight API.
    if settings.admin_dashboard_enabled:
        try:
            from app.admin import setup_admin

            setup_admin(app)
        except Exception as exc:  # noqa: BLE001
            print(f"admin dashboard setup skipped (non-fatal): {exc!r}")
    # Sprint 21B — reconciliation engine API (read-only drift detection).
    if settings.reconciliation_enabled:
        try:
            from app.reconciliation import setup_reconciliation

            setup_reconciliation(app)
        except Exception as exc:  # noqa: BLE001
            print(f"reconciliation setup skipped (non-fatal): {exc!r}")
    # Sprint 21C — position recovery engine API.
    if settings.position_recovery_enabled:
        try:
            from app.recovery import setup_recovery

            setup_recovery(app)
        except Exception as exc:  # noqa: BLE001
            print(f"recovery setup skipped (non-fatal): {exc!r}")
    # Sprint 21D — order failure / retry engine API.
    if settings.order_failure_engine_enabled:
        try:
            from app.order_failures import setup_order_failures

            setup_order_failures(app)
        except Exception as exc:  # noqa: BLE001
            print(f"order failure engine setup skipped (non-fatal): {exc!r}")
    # Sprint 21E — net-PnL accounting engine API.
    if settings.accounting_enabled:
        try:
            from app.accounting import setup_accounting

            setup_accounting(app)
        except Exception as exc:  # noqa: BLE001
            print(f"accounting setup skipped (non-fatal): {exc!r}")
    # Multi-user Live Beta — membership API (controlled live access).
    if settings.live_beta_enabled:
        try:
            from app.live_beta import setup_live_beta

            setup_live_beta(app)
        except Exception as exc:  # noqa: BLE001
            print(f"live beta setup skipped (non-fatal): {exc!r}")
    # V12 — mount the SaaS portal shell at /app (static; APIs stay flag-gated).
    try:
        from app.dashboard.saas_app import setup_saas_app

        setup_saas_app(app)
    except Exception as exc:  # noqa: BLE001
        print(f"saas portal setup skipped (non-fatal): {exc!r}")
    # Phase 4 — modular dashboard routers (handlers extracted from this file).
    from app.dashboard.routes import (
        admin_router,
        analytics_router,
        auth_router,
        paper_router,
        public_router,
        system_router,
    )

    for _r in (
        public_router,
        system_router,
        analytics_router,
        paper_router,
        admin_router,
        auth_router,
    ):
        app.include_router(_r.router)
    return app


# ═════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═════════════════════════════════════════════════════════════════
