"""
Structured rotating-file logging using loguru. Imported once at startup,
then every module can use `from app.utils.logger import logger`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from app.config import settings

_INITIALIZED = False


def setup_logging() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)

    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level:<8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # stdout — all levels
    logger.add(
        sys.stdout,
        level=settings.log_level.upper(),
        format=fmt,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    # master app log
    logger.add(
        os.path.join(settings.log_dir, "app.log"),
        level=settings.log_level.upper(),
        format=fmt,
        rotation="20 MB",
        retention="14 days",
        compression="zip",
        enqueue=True,
    )

    # errors only
    logger.add(
        os.path.join(settings.log_dir, "errors.log"),
        level="ERROR",
        format=fmt,
        rotation="20 MB",
        retention="30 days",
        compression="zip",
        enqueue=True,
    )

    # per-subsystem logs
    _subsystems = {
        "scanner.log":   "app.scanner",
        "telegram.log":  "app.telegram_bot",
        "database.log":  "app.database",
        "websocket.log": "app.market_data.ws_engine",
    }
    for filename, module_prefix in _subsystems.items():
        logger.add(
            os.path.join(settings.log_dir, filename),
            level="DEBUG",
            format=fmt,
            rotation="10 MB",
            retention="7 days",
            compression="zip",
            enqueue=True,
            filter=lambda r, p=module_prefix: r["name"].startswith(p),
        )

    _INITIALIZED = True


setup_logging()

__all__ = ["logger", "setup_logging"]
