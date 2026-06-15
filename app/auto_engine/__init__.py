"""
Sprint 20D — Auto Trading Engine (DEMO MODE ONLY).

Executes signals automatically against per-user PAPER accounts (Sprint 20B).
No real orders are ever placed. Feature-flagged behind AUTO_TRADE_DEMO_ENABLED;
per-user opt-in via AutoTradeConfig.enabled or PaperAccount.auto_follow.

Call setup_auto_engine(app) from create_app() to mount the config/status API.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_auto_engine(app: FastAPI) -> None:
    """Idempotently mount the auto-trading router + error handler onto `app`."""
    if getattr(app.state, "_auto_installed", False):
        return

    from app.auth.service import AuthError
    from app.auto_engine.router import router as auto_router

    async def _auth_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"detail": getattr(exc, "detail", str(exc))},
        )

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.include_router(auto_router)
    app.state._auto_installed = True
