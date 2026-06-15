"""
Sprint 20B — per-user paper (demo) futures trading.

Feature-flagged: inactive unless PAPER_TRADING_ENABLED=true. Endpoints require
an authenticated user (Sprint 20A). Call setup_paper(app) from create_app().
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_paper(app: FastAPI) -> None:
    """Idempotently mount the paper router + error handlers onto `app`."""
    if getattr(app.state, "_paper_installed", False):
        return

    from app.auth.service import AuthError
    from app.paper_engine.router import debug_router
    from app.paper_engine.router import router as paper_router
    from app.paper_engine.service import PaperError

    async def _paper_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"detail": getattr(exc, "detail", str(exc))},
        )

    async def _auth_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"detail": getattr(exc, "detail", str(exc))},
        )

    app.add_exception_handler(PaperError, _paper_error_handler)
    # Ensure AuthError -> 401 even if the auth router itself wasn't mounted.
    app.add_exception_handler(AuthError, _auth_error_handler)
    app.include_router(paper_router)
    app.include_router(debug_router)
    app.state._paper_installed = True
