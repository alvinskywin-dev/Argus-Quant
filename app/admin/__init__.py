"""
Sprint 20H — Admin Dashboard.

ADMIN-only platform oversight: a one-call overview rollup, paginated user list,
per-user detail, a live audit feed, and suspend/activate moderation. Read-
oriented; it never exposes decrypted exchange credentials and reuses the 20E
global emergency stop rather than duplicating it.

Feature-flagged behind ADMIN_DASHBOARD_ENABLED (default off). Call
setup_admin(app) from create_app().
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_admin(app: FastAPI) -> None:
    """Idempotently mount the admin router + error handlers onto `app`."""
    if getattr(app.state, "_admin_installed", False):
        return

    from app.admin.router import router as admin_router
    from app.admin.service import AdminError
    from app.auth.service import AuthError

    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    async def _admin_error_handler(_request: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.add_exception_handler(AdminError, _admin_error_handler)
    app.include_router(admin_router)
    app.state._admin_installed = True
