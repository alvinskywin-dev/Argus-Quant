"""
Sprint 21E — net-PnL accounting engine.

Feature-flagged behind ACCOUNTING_ENABLED. Computes gross/commission/funding/
slippage/net PnL per trade and rolls daily aggregates, keeping MOCK and LIVE
separate. Call setup_accounting(app) from create_app().
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_accounting(app: FastAPI) -> None:
    if getattr(app.state, "_accounting_installed", False):
        return
    from app.accounting.router import router as acct_router
    from app.auth.service import AuthError

    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.include_router(acct_router)
    app.state._accounting_installed = True
