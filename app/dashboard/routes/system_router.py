"""system router — extracted from server.py (Phase 4).

Handlers moved verbatim; shared helpers/views/templates are imported
from app.dashboard.server. Wired via create_app().include_router().
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy import desc, select
from sqlalchemy import func as _sqlfunc

from app.config import settings
from app.dashboard.server import (
    _MTF_STRATEGY,
    _MTF_TIMEFRAMES,
    _boot_time,
    _get_stats,
    _health_page_html,
    _is_logged_in,
)
from app.database.models import FundingRateSnapshot, OpenInterestSnapshot, Signal
from app.database.session import SessionLocal
from app.market_data import universe
from app.market_data.ws_engine import ws_health
from app.utils.observability import METRICS
from app.utils.timezone import normalize_utc_iso

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
async def health():
    return HTMLResponse(_health_page_html())


@router.get("/api/health")
async def api_health():
    """
    Sprint 5 Health Center API.
    Returns per-service status for: dashboard, database, redis, binance,
    telegram, scanner, worker, scheduler — plus activity metrics.
    Backward-compat fields (uptime_sec, components, config) are preserved.
    """
    now_ts = datetime.now(timezone.utc)
    checked = now_ts.isoformat()
    uptime = round(time.time() - _boot_time)

    def _svc(
        ok: bool,
        *,
        error: str | None = None,
        latency_ms: float | None = None,
        detail: str | None = None,
        **extra,
    ) -> dict:
        s: dict = {
            "ok": ok,
            "status": "ONLINE" if ok else "OFFLINE",
            "checked_at": checked,
            "latency_ms": latency_ms,
            "error": error,
        }
        if detail:
            s["detail"] = detail
        s.update(extra)
        return s

    # ── Dashboard ─────────────────────────────────────────────────────
    svc_dashboard = _svc(True, detail=f"port {settings.dashboard_port}", uptime_seconds=uptime)

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
    svc_database = _svc(db_ok, latency_ms=db_lat, error=db_err, detail="PostgreSQL (asyncpg)")

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
    svc_redis = _svc(
        redis_ok, latency_ms=redis_lat, error=redis_err, detail="price & cooldown cache"
    )

    # ── Binance WebSocket price feed ──────────────────────────────────
    wsh = ws_health()
    binance_ok = bool(wsh.get("ok"))
    feed_age = wsh.get("last_update_age_sec")
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
        last_scan_iso = datetime.fromtimestamp(last_scan_epoch, tz=timezone.utc).isoformat()
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
    signals_today = 0
    try:
        today_start = now_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        async with SessionLocal() as s:
            # most recent signal
            res = await s.execute(
                select(Signal.created_at).order_by(desc(Signal.created_at)).limit(1)
            )
            ts = res.scalar_one_or_none()
            if ts:
                last_signal_iso = ts.isoformat()

            # signals today
            cnt_res = await s.execute(
                select(_sqlfunc.count(Signal.id)).where(Signal.created_at >= today_start)
            )
            signals_today = int(cnt_res.scalar() or 0)
    except Exception:
        pass

    services = {
        "dashboard": svc_dashboard,
        "database": svc_database,
        "redis": svc_redis,
        "binance": svc_binance,
        "telegram": svc_telegram,
        "scanner": svc_scanner,
        "worker": svc_worker,
        "scheduler": svc_scheduler,
    }

    overall_ok = db_ok and redis_ok

    return JSONResponse(
        {
            # ── Sprint 5 schema ────────────────────────────────────────────
            "ok": overall_ok,
            "checked_at": checked,
            "uptime_seconds": uptime,
            "services": services,
            "last_scan_time": last_scan_iso,
            "last_signal_time": last_signal_iso,
            "signals_today": signals_today,
            "errors_today": 0,
            # ── backward-compat (admin dashboard JS reads these) ───────────
            "brand": "ARGUS QUANT",
            "uptime_sec": uptime,
            "components": {
                "dashboard": {"ok": True, "detail": f"port {settings.dashboard_port}"},
                "database": {"ok": db_ok, "latency_ms": db_lat or -1},
                "redis": {"ok": redis_ok, "latency_ms": redis_lat or -1},
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
    )


@router.get("/status")
async def status_route():
    wsh = ws_health()
    return {
        "status": "ok",
        "uptime_sec": round(time.time() - _boot_time),
        "universe": len(universe.symbols),
        "websocket": wsh,
        "config": {
            "min_confidence": settings.min_confidence,
            "scan_interval_sec": settings.scan_interval_sec,
            "max_signals_per_hour": settings.max_signals_per_hour,
            "timeframes": settings.timeframes,
        },
    }


@router.get("/api/oi/status")
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
                select(OpenInterestSnapshot).join(
                    subq,
                    (OpenInterestSnapshot.symbol == subq.c.symbol)
                    & (OpenInterestSnapshot.created_at == subq.c.latest),
                )
            )
            snapshots = res.scalars().all()

        bullish = sum(1 for s in snapshots if s.oi_score > 0)
        bearish = sum(1 for s in snapshots if s.oi_score < 0)
        neutral = sum(1 for s in snapshots if s.oi_score == 0)
        total = len(snapshots)

        recent = sorted(snapshots, key=lambda s: s.created_at, reverse=True)[:20]

        return JSONResponse(
            {
                "open_interest_status": "active" if total > 0 else "no_data",
                "total_symbols": total,
                "bullish_oi": bullish,
                "bearish_oi": bearish,
                "neutral_oi": neutral,
                "snapshots": [
                    {
                        "symbol": s.symbol,
                        "open_interest": s.open_interest,
                        "oi_change_5m": s.oi_change_5m,
                        "oi_change_15m": s.oi_change_15m,
                        "oi_change_1h": s.oi_change_1h,
                        "price_change": s.price_change_pct,
                        "oi_score": s.oi_score,
                        "time": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
                        "time_iso": normalize_utc_iso(s.created_at),
                    }
                    for s in recent
                ],
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/funding/status")
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
                select(FundingRateSnapshot).join(
                    subq,
                    (FundingRateSnapshot.symbol == subq.c.symbol)
                    & (FundingRateSnapshot.created_at == subq.c.latest),
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

        return JSONResponse(
            {
                "funding_status": "active" if total > 0 else "no_data",
                "total_symbols": total,
                "extreme_positive_funding": extreme_pos,
                "extreme_negative_funding": extreme_neg,
                "neutral_funding": neutral_cnt,
                "positive_funding": positive_cnt,
                "negative_funding": negative_cnt,
                "snapshots": [
                    {
                        "symbol": s.symbol,
                        "funding_rate": s.funding_rate,
                        "funding_pct": round(s.funding_rate * 100, 5),
                        "classification": s.classification,
                        "time": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
                        "time_iso": normalize_utc_iso(s.created_at),
                    }
                    for s in recent
                ],
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/metrics")
async def metrics():
    wsh = ws_health()
    m = METRICS.snapshot()
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
        "# HELP alpha_radar_http_requests_total HTTP requests by status class",
        "# TYPE alpha_radar_http_requests_total counter",
    ]
    for status_class, count in sorted(m["http_requests_total"].items()):
        lines.append(f'alpha_radar_http_requests_total{{status="{status_class}"}} {count}')
    lines += [
        "# HELP alpha_radar_http_request_errors_total HTTP 5xx responses",
        "# TYPE alpha_radar_http_request_errors_total counter",
        f"alpha_radar_http_request_errors_total {m['http_requests_errors_total']}",
        "# HELP alpha_radar_http_requests_in_flight In-flight HTTP requests",
        "# TYPE alpha_radar_http_requests_in_flight gauge",
        f"alpha_radar_http_requests_in_flight {m['http_requests_in_flight']}",
        "# HELP alpha_radar_http_request_duration_seconds_sum Total request time",
        "# TYPE alpha_radar_http_request_duration_seconds_sum counter",
        f"alpha_radar_http_request_duration_seconds_sum {m['http_request_duration_sum']}",
        "# HELP alpha_radar_http_request_duration_seconds_count Completed requests",
        "# TYPE alpha_radar_http_request_duration_seconds_count counter",
        f"alpha_radar_http_request_duration_seconds_count {m['http_request_duration_count']}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/api/system/metrics")
async def api_system_metrics():
    """System-level signal metrics for monitoring and external integrations."""
    try:
        async with SessionLocal() as session:
            total_res = await session.execute(
                select(_sqlfunc.count(Signal.id)).where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
            )
            signals_total = int(total_res.scalar() or 0)

            open_res = await session.execute(
                select(_sqlfunc.count(Signal.id)).where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status == "OPEN",
                )
            )
            open_signals = int(open_res.scalar() or 0)

            closed_res = await session.execute(
                select(_sqlfunc.count(Signal.id)).where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
                )
            )
            closed_signals = int(closed_res.scalar() or 0)

            wins_res = await session.execute(
                select(_sqlfunc.count(Signal.id)).where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                    Signal.status.in_(["TP1", "TP2", "TP3"]),
                )
            )
            wins = int(wins_res.scalar() or 0)

        winrate_closed = round(wins / closed_signals * 100, 1) if closed_signals > 0 else None

        return JSONResponse(
            {
                "ok": True,
                "signals_total": signals_total,
                "open_signals": open_signals,
                "closed_signals": closed_signals,
                "winrate_closed": winrate_closed,
                "universe": len(universe.symbols),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/prices")
async def api_prices(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(ws_health())


@router.get("/api/dashboard")
async def api_dashboard(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse(await _get_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
