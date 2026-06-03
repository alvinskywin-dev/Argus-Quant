"""
Multi-user Live Beta — API.

User endpoints request access and read their own membership; admin endpoints
list members and approve / reject / suspend. Mounted only when
LIVE_BETA_ENABLED=true (see create_app).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth.deps import get_current_user, require_role
from app.database.models import AuthUser
from app.database.session import get_session
from app.live_beta import service
from app.live_beta.schemas import BetaAdminActionIn, BetaRequestIn

router = APIRouter(prefix="/api/live-beta", tags=["live-beta"])


def _err(exc: service.LiveBetaError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ── user ───────────────────────────────────────────────────────────


@router.post("/request", response_model=dict, status_code=201)
async def request_access(body: BetaRequestIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            m = await service.request_access(
                db, user_id=user.id, invite_code=body.invite_code, accept_risk=body.accept_risk
            )
            return service.member_dict(m)
    except service.LiveBetaError as exc:
        return _err(exc)


@router.get("/status", response_model=dict)
async def my_status(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        m = await service.get_member(db, user.id)
        if m is None:
            return {"member": False, "beta_open": service.beta_enabled()}
        return {"member": True, "beta_open": service.beta_enabled(), **service.member_dict(m)}


# ── admin ──────────────────────────────────────────────────────────


@router.get("/admin/members", response_model=dict)
async def list_members(_admin: AuthUser = Depends(require_role("ADMIN"))):
    async with get_session() as db:
        members = await service.list_members(db)
        return {"total": len(members), "members": [service.member_dict(m) for m in members]}


@router.post("/admin/approve", response_model=dict)
async def approve(body: BetaAdminActionIn, admin: AuthUser = Depends(require_role("ADMIN"))):
    try:
        async with get_session() as db:
            m = await service.approve(db, admin_id=admin.id, user_id=body.user_id)
            return service.member_dict(m)
    except service.LiveBetaError as exc:
        return _err(exc)


@router.post("/admin/reject", response_model=dict)
async def reject(body: BetaAdminActionIn, admin: AuthUser = Depends(require_role("ADMIN"))):
    try:
        async with get_session() as db:
            m = await service.reject(
                db, admin_id=admin.id, user_id=body.user_id, reason=body.reason
            )
            return service.member_dict(m)
    except service.LiveBetaError as exc:
        return _err(exc)


@router.post("/admin/suspend", response_model=dict)
async def suspend(body: BetaAdminActionIn, admin: AuthUser = Depends(require_role("ADMIN"))):
    try:
        async with get_session() as db:
            m = await service.suspend(
                db, admin_id=admin.id, user_id=body.user_id, reason=body.reason
            )
            return service.member_dict(m)
    except service.LiveBetaError as exc:
        return _err(exc)
