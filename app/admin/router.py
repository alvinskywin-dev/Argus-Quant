"""
Sprint 20H — Admin Dashboard API.

Mounted only when ADMIN_DASHBOARD_ENABLED=true. EVERY route requires the ADMIN
role (require_role("ADMIN")). Read-oriented platform oversight plus user
suspend/activate moderation. Decrypted credentials are never exposed.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.admin import service
from app.admin.schemas import SetUserStatusIn
from app.auth.deps import require_role
from app.database.models import AuthUser
from app.database.session import get_session

router = APIRouter(prefix="/api/admin", tags=["admin-dashboard"])

# Every endpoint in this router is ADMIN-only.
_admin = Depends(require_role("ADMIN"))


@router.get("/overview", response_model=dict)
async def overview(user: AuthUser = _admin):
    async with get_session() as db:
        return await service.overview(db)


@router.get("/users", response_model=dict)
async def list_users(
    user: AuthUser = _admin,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
):
    async with get_session() as db:
        return await service.list_users(db, limit=limit, offset=offset, status=status, role=role)


@router.get("/users/{user_id}", response_model=dict)
async def user_detail(user_id: int, user: AuthUser = _admin):
    async with get_session() as db:
        return await service.user_detail(db, user_id)


@router.put("/users/{user_id}/status", response_model=dict)
async def set_user_status(user_id: int, body: SetUserStatusIn, user: AuthUser = _admin):
    async with get_session() as db:
        return await service.set_user_status(
            db, admin_id=user.id, user_id=user_id, status=body.status
        )


@router.get("/audit", response_model=list)
async def audit_feed(
    user: AuthUser = _admin,
    limit: int = Query(100, ge=1, le=500),
    user_id: Optional[int] = Query(None),
):
    async with get_session() as db:
        return await service.audit_feed(db, limit=limit, user_id=user_id)
