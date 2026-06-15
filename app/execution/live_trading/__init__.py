"""
Sprint 20F — Live trading (Binance USDT-M Futures).

The API can be mounted (LIVE_TRADING_API_ENABLED) to exercise the full pipeline
in MOCK mode; REAL orders still require the execution gate
(LIVE_TRADING_ENABLED=true AND MOCK_EXCHANGE_MODE=false). Call setup_live(app)
from create_app().
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_live(app: FastAPI) -> None:
    """Idempotently mount the live-trading router + error handlers onto `app`."""
    if getattr(app.state, "_live_installed", False):
        return

    from app.auth.service import AuthError
    from app.execution.live_trading.router import router as live_router
    from app.execution.live_trading.service import LiveTradingError

    async def _auth_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"detail": getattr(exc, "detail", str(exc))},
        )

    async def _live_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 500),
            content={"detail": getattr(exc, "detail", str(exc))},
        )

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.add_exception_handler(LiveTradingError, _live_error_handler)
    app.include_router(live_router)
    app.state._live_installed = True
