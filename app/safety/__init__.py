"""
Sprint 20E — Real Trading Safety Layer.

Account-protection guards (loss limits, correlation caps, cooldown, loss-streak)
plus user and admin kill switches. The check runs inside the auto engine before
any open; the API lets users/admins configure limits and trigger emergency stops.

Feature-flagged behind SAFETY_LAYER_ENABLED (on by default). Call
setup_safety(app) from create_app().
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_safety(app: FastAPI) -> None:
    """Idempotently mount the safety routers + error handler onto `app`."""
    if getattr(app.state, "_safety_installed", False):
        return

    from app.auth.service import AuthError
    from app.safety.router import admin_router, router
    from app.safety.service import SafetyError

    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    async def _safety_error_handler(_request: Request, exc: SafetyError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(AuthError, _auth_error_handler)  # type: ignore[arg-type]  # FastAPI: handler typed for its specific exc subtype
    app.add_exception_handler(SafetyError, _safety_error_handler)  # type: ignore[arg-type]  # FastAPI: handler typed for its specific exc subtype
    app.include_router(router)
    app.include_router(admin_router)
    app.state._safety_installed = True
