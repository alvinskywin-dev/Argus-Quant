"""
Sprint 21B — execution reconciliation engine (DB ↔ exchange drift detection).

Feature-flagged behind RECONCILIATION_ENABLED. Detection is strictly read-only:
it inspects positions/open-orders and records ReconciliationIssue rows. It never
opens, closes, or cancels orders. Call setup_reconciliation(app) from create_app.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_reconciliation(app: FastAPI) -> None:
    if getattr(app.state, "_reconciliation_installed", False):
        return
    from app.auth.service import AuthError
    from app.reconciliation.router import router as recon_router

    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(AuthError, _auth_error_handler)  # type: ignore[arg-type]  # FastAPI: handler typed for its specific exc subtype
    app.include_router(recon_router)
    app.state._reconciliation_installed = True
