"""
Sprint 20A — multi-user SaaS authentication.

Feature-flagged: nothing here is active unless AUTH_ENABLED=true.
Call setup_auth(app) from the dashboard's create_app() to mount the router
and install the AuthError -> JSON exception handler.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_auth(app: FastAPI) -> None:
    """Idempotently mount the auth router + exception handler onto `app`."""
    if getattr(app.state, "_auth_installed", False):
        return

    from app.auth.router import router as auth_router
    from app.auth.service import AuthError

    async def _auth_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"detail": getattr(exc, "detail", str(exc))},
        )

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.include_router(auth_router)
    app.state._auth_installed = True
