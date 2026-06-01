"""
Sprint 21D — order failure / retry engine.

Feature-flagged behind ORDER_FAILURE_ENGINE_ENABLED. Classifies live-order
failures, applies a bounded retry policy, prevents duplicate entries via an
idempotency key, and trips a per-user circuit breaker. Call
setup_order_failures(app) from create_app().
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_order_failures(app: FastAPI) -> None:
    if getattr(app.state, "_order_failures_installed", False):
        return
    from app.auth.service import AuthError
    from app.order_failures.router import router as of_router

    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.include_router(of_router)
    app.state._order_failures_installed = True
