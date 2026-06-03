"""
Sprint 20A — FastAPI dependencies for protected routes.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import security, service
from app.database.models import AuthUser
from app.database.session import get_session

_bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def client_device(request: Request) -> Optional[str]:
    return request.headers.get("user-agent")


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> AuthUser:
    if credentials is None or not credentials.credentials:
        raise service.AuthError(401, "Not authenticated")
    try:
        payload = security.decode_access_token(credentials.credentials)
    except security.TokenError:
        raise service.AuthError(401, "Invalid or expired token")

    sub = payload.get("sub")
    if not sub:
        raise service.AuthError(401, "Invalid token subject")

    async with get_session() as db:
        user = await service.get_user_by_id(db, int(sub))
    if user is None:
        raise service.AuthError(401, "User not found")
    if user.status == "SUSPENDED":
        raise service.AuthError(403, "Account suspended")
    return user


def require_role(*roles: str):
    """Dependency factory: require the current user to hold one of `roles`."""

    async def _checker(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role not in roles:
            raise service.AuthError(403, "Insufficient permissions")
        return user

    return _checker
