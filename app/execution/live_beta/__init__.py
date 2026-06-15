"""
Multi-user Live Beta — controlled live-trading access for a small allowlist.

Feature-flagged: nothing here is active unless LIVE_BETA_ENABLED=true. Mounts a
membership API (request access / admin approval) and exposes a `beta_gate`
reusable by the live execution path. It never places orders itself; it only
decides whether a given user may.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_live_beta(app: FastAPI) -> None:
    """Idempotently mount the live-beta router + error handler onto `app`."""
    if getattr(app.state, "_live_beta_installed", False):
        return

    from app.execution.live_beta.router import router as beta_router
    from app.execution.live_beta.service import LiveBetaError

    async def _beta_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"detail": getattr(exc, "detail", str(exc))},
        )

    app.add_exception_handler(LiveBetaError, _beta_error_handler)
    app.include_router(beta_router)
    app.state._live_beta_installed = True
