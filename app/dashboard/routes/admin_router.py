"""admin router — extracted from server.py (Phase 4).

Handlers moved verbatim; shared helpers/views/templates are imported
from app.dashboard.server. Wired via create_app().include_router().
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from app.dashboard.server import (
    _ADMIN_HTML,
    _PLATFORM_ADMIN_HTML,
    _is_logged_in,
)
from app.database.models import AffiliateClick
from app.database.repo import get_active_signals_summary
from app.database.session import SessionLocal, get_session

router = APIRouter()


@router.get("/api/admin/affiliate-stats")
async def affiliate_stats(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from sqlalchemy import func as sqlfunc

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


@router.get("/api/admin/active-signals")
async def admin_active_signals(request: Request):
    """Return all currently active (OPEN/ACTIVE/PENDING) signals for duplicate monitoring."""
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        rows = await get_active_signals_summary()
        return JSONResponse({"active": rows, "count": len(rows)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/performance/rebuild")
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


@router.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request):
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(_ADMIN_HTML)


@router.get("/admin/platform", response_class=HTMLResponse)
async def admin_platform_page(request: Request):
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(_PLATFORM_ADMIN_HTML)


@router.get("/api/saas-admin/overview")
async def saas_admin_overview(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from app.admin import service as admin_service

        async with get_session() as db:
            return JSONResponse(await admin_service.overview(db))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/saas-admin/safety-overview")
async def saas_admin_safety_overview(request: Request):
    """Sprint 21 — read-only live-safety snapshot for the admin platform."""
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    out: dict = {
        "reconciliation": {},
        "recovery": {},
        "order_failures": {},
        "accounting": {},
        "live_gate": {},
    }
    try:
        from app.live_trading.service import gate_status

        out["live_gate"] = gate_status()
    except Exception as exc:  # noqa: BLE001
        out["live_gate"] = {"error": str(exc)[:120]}
    async with get_session() as db:
        try:
            from app.reconciliation import report as recon_report

            out["reconciliation"] = await recon_report.summary(db)
        except Exception as exc:  # noqa: BLE001
            out["reconciliation"] = {"error": str(exc)[:120]}
        try:
            from app.recovery.status import recovery_status

            out["recovery"] = await recovery_status(db)
        except Exception as exc:  # noqa: BLE001
            out["recovery"] = {"error": str(exc)[:120]}
        try:
            from app.order_failures import service as of_service

            out["order_failures"] = await of_service.summary(db)
        except Exception as exc:  # noqa: BLE001
            out["order_failures"] = {"error": str(exc)[:120]}
        try:
            from app.accounting import service as acct_service

            out["accounting"] = await acct_service.summary(db)
        except Exception as exc:  # noqa: BLE001
            out["accounting"] = {"error": str(exc)[:120]}
    return JSONResponse(out)


@router.get("/api/saas-admin/users")
async def saas_admin_users(
    request: Request, limit: int = 100, offset: int = 0, status: str = "", role: str = ""
):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from app.admin import service as admin_service

        async with get_session() as db:
            return JSONResponse(
                await admin_service.list_users(
                    db, limit=limit, offset=offset, status=status or None, role=role or None
                )
            )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/saas-admin/users/{user_id}")
async def saas_admin_user_detail(request: Request, user_id: int):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from app.admin import service as admin_service

        async with get_session() as db:
            return JSONResponse(await admin_service.user_detail(db, user_id))
    except admin_service.AdminError as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/saas-admin/audit")
async def saas_admin_audit(request: Request, limit: int = 100):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from app.admin import service as admin_service

        async with get_session() as db:
            return JSONResponse(await admin_service.audit_feed(db, limit=limit))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/saas-admin/users/{user_id}/status")
async def saas_admin_set_status(request: Request, user_id: int):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        new_status = str(body.get("status", "")).upper()
        from app.admin import service as admin_service

        async with get_session() as db:
            # admin_id=0: the dashboard operator is not a SaaS user, so the
            # self-suspend guard never applies here.
            result = await admin_service.set_user_status(
                db, admin_id=0, user_id=user_id, status=new_status
            )
        return JSONResponse(result)
    except admin_service.AdminError as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
