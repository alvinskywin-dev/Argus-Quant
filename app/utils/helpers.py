"""Tiny shared helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%d %H:%M UTC")


def safe_pct(numer: float, denom: float) -> float:
    if denom == 0:
        return 0.0
    return (numer / denom) * 100.0


async def run_forever(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    name: str,
    delay_on_error: float = 5.0,
) -> None:
    """
    Generic supervisor: keep running an awaitable, log + sleep on crash.
    """
    from app.utils.logger import logger

    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            logger.info(f"[{name}] cancelled — exiting supervisor")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{name}] crashed: {exc} — restarting in {delay_on_error}s")
            await asyncio.sleep(delay_on_error)


def fmt_price(p: float) -> str:
    """Format a price with reasonable decimals."""
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


def fmt_pct(p: float) -> str:
    return f"{p:+.2f}%"
